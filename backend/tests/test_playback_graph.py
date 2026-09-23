"""
Unit and integration tests for PlaybackGraph (State Graph / FSM architecture).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services.scraper.playback_graph import (
    PlaybackGraph,
    PlaybackState,
    PlaybackListenerScope,
    poll_until_playing,
)


@pytest.mark.asyncio
async def test_poll_until_playing_returns_true_fast():
    """Verify smart polling exits early when playback starts without waiting for timeout."""
    call_count = 0

    async def mock_is_playing(page):
        nonlocal call_count
        call_count += 1
        return call_count >= 2

    mock_page = MagicMock()
    start_time = asyncio.get_running_loop().time()
    result = await poll_until_playing(mock_page, mock_is_playing, timeout_s=2.0, interval_s=0.05)
    elapsed = asyncio.get_running_loop().time() - start_time

    assert result is True
    assert call_count >= 2
    assert elapsed < 0.8  # Exited well before the 2.0s timeout


@pytest.mark.asyncio
async def test_poll_until_playing_returns_false_on_timeout():
    """Verify smart polling respects timeout and returns False if never playing."""
    async def mock_never_playing(page):
        return False

    mock_page = MagicMock()
    result = await poll_until_playing(mock_page, mock_never_playing, timeout_s=0.2, interval_s=0.05)
    assert result is False


def test_playback_state_strategy_caching():
    """Verify strategy memory caching and invalidation."""
    mock_page = MagicMock()
    mock_llm = MagicMock()
    state = PlaybackState(page=mock_page, llm_manager=mock_llm)

    assert state.preferred_strategy is None
    state.remember_strategy("force_play_js", "video.play() kick", {})
    assert state.preferred_strategy == "force_play_js"
    assert state.preferred_failures == 0

    state.preferred_failures = 2
    state.invalidate_strategy()
    assert state.preferred_strategy is None
    assert state.preferred_failures == 0


@pytest.mark.asyncio
async def test_playback_listener_scope_cleanup():
    """Verify PlaybackListenerScope registers and cleans up listeners without leaks."""
    mock_page = MagicMock()
    mock_context = MagicMock()
    mock_page.context = mock_context

    state = PlaybackState(page=mock_page, llm_manager=MagicMock())

    from unittest.mock import ANY

    async with PlaybackListenerScope(state):
        mock_page.on.assert_any_call("dialog", ANY)
        mock_page.on.assert_any_call("framenavigated", ANY)
        mock_context.on.assert_any_call("page", ANY)

    mock_page.remove_listener.assert_any_call("dialog", ANY)
    mock_page.remove_listener.assert_any_call("framenavigated", ANY)
    mock_context.remove_listener.assert_any_call("page", ANY)


@pytest.mark.asyncio
async def test_playback_graph_autoplay_fast_path():
    """Verify that if autoplay is active, the graph confirms fullscreen and exits without calling LLM."""
    mock_page = MagicMock()
    mock_llm = AsyncMock()

    is_playing_mock = AsyncMock(return_value=True)
    pre_pass_mock = AsyncMock(return_value=0)
    force_play_mock = AsyncMock(return_value=False)
    try_direct_play_mock = AsyncMock(return_value=False)
    media_session_mock = AsyncMock(return_value=False)
    request_fullscreen_mock = AsyncMock()

    graph = PlaybackGraph(
        is_playing_fn=is_playing_mock,
        pre_pass_fn=pre_pass_mock,
        force_play_js_fn=force_play_mock,
        try_direct_play_fn=try_direct_play_mock,
        media_session_fn=media_session_mock,
        request_fullscreen_fn=request_fullscreen_mock,
    )

    state = PlaybackState(page=mock_page, llm_manager=mock_llm, max_attempts=3)
    result = await graph.run(state)

    assert result is True
    assert mock_llm.execute.call_count == 0
    request_fullscreen_mock.assert_awaited()
    assert any("DetectAutoplay" in h for h in state.history)


@pytest.mark.asyncio
async def test_playback_graph_fast_heuristics_success():
    """Verify force_play_js triggers playback without vision LLM call."""
    mock_page = MagicMock()
    mock_llm = AsyncMock()

    # Initial autoplay False, but force_play_js returns True and causes is_playing to become True
    play_state = {"playing": False}

    async def mock_is_playing(page):
        return play_state["playing"]

    async def mock_force_play(page):
        play_state["playing"] = True
        return True

    graph = PlaybackGraph(
        is_playing_fn=mock_is_playing,
        pre_pass_fn=AsyncMock(return_value=0),
        force_play_js_fn=mock_force_play,
        try_direct_play_fn=AsyncMock(return_value=False),
        media_session_fn=AsyncMock(return_value=False),
        request_fullscreen_fn=AsyncMock(),
    )

    state = PlaybackState(page=mock_page, llm_manager=mock_llm, max_attempts=2)
    result = await graph.run(state)

    assert result is True
    assert mock_llm.execute.call_count == 0
    assert state.preferred_strategy == "force_play_js"


@pytest.mark.asyncio
async def test_playback_graph_llm_action_with_popup_recovery():
    """Verify LLM vision planning triggers a click, recovers from popup, and confirms playback."""
    mock_page = MagicMock()
    mock_page.viewport_size = {"width": 1920, "height": 1080}
    mock_page.frames = [mock_page]
    mock_page.content = AsyncMock(return_value="<html><body><video></video></body></html>")

    mock_llm = MagicMock()
    mock_llm.execute = AsyncMock(return_value={
        "action_selector": "#custom-play-btn",
        "pixel_x": 960,
        "pixel_y": 540,
        "reason": "Click central custom play button",
    })

    play_state = {"playing": False}

    async def mock_is_playing(page):
        return play_state["playing"]

    graph = PlaybackGraph(
        is_playing_fn=mock_is_playing,
        pre_pass_fn=AsyncMock(return_value=0),
        force_play_js_fn=AsyncMock(return_value=False),
        try_direct_play_fn=AsyncMock(return_value=False),
        media_session_fn=AsyncMock(return_value=False),
        request_fullscreen_fn=AsyncMock(),
    )

    with patch("app.services.scraper.playback_graph.ComputerUse") as mock_cu_cls:
        mock_cu_instance = MagicMock()
        mock_cu_instance.screenshot = AsyncMock(return_value=b"fake-jpeg")
        mock_cu_instance.aria_snapshot = AsyncMock(return_value="button 'Play'")
        mock_cu_instance.aria_snapshot_for_frame = AsyncMock(return_value="")

        async def mock_click(selector):
            # First click sets playing True
            play_state["playing"] = True
            return True

        mock_cu_instance.click_by_selector = AsyncMock(side_effect=mock_click)
        mock_cu_cls.return_value = mock_cu_instance

        state = PlaybackState(page=mock_page, llm_manager=mock_llm, max_attempts=2)
        result = await graph.run(state)

        assert result is True
        assert mock_llm.execute.call_count == 1
        assert state.last_selector == "#custom-play-btn"
        assert state.preferred_strategy == "llm_selector"


@pytest.mark.asyncio
async def test_playback_graph_exhausted_attempts_returns_false():
    """Verify graph terminates and returns False when maximum attempts are exhausted."""
    mock_page = MagicMock()
    mock_page.viewport_size = {"width": 1920, "height": 1080}
    mock_page.frames = [mock_page]
    mock_page.content = AsyncMock(return_value="<html></html>")

    mock_llm = MagicMock()
    mock_llm.execute = AsyncMock(return_value={"action_selector": None, "pixel_x": None})

    graph = PlaybackGraph(
        is_playing_fn=AsyncMock(return_value=False),
        pre_pass_fn=AsyncMock(return_value=0),
        force_play_js_fn=AsyncMock(return_value=False),
        try_direct_play_fn=AsyncMock(return_value=False),
        media_session_fn=AsyncMock(return_value=False),
        request_fullscreen_fn=AsyncMock(),
    )

    with patch("app.services.scraper.playback_graph.ComputerUse") as mock_cu_cls:
        mock_cu_instance = MagicMock()
        mock_cu_instance.screenshot = AsyncMock(return_value=b"fake-jpeg")
        mock_cu_instance.aria_snapshot = AsyncMock(return_value="")
        mock_cu_cls.return_value = mock_cu_instance

        state = PlaybackState(page=mock_page, llm_manager=mock_llm, max_attempts=2)
        result = await graph.run(state)

        assert result is False
