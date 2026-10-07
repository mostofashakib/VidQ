"""Tests for comparing two results by what their video shows."""

import pytest

from app.services.search.fingerprint import FrameMatcher, dhash, frame_command, hamming, same_content
from app.services.search.models import SearchResult
from app.services.search.streams import ResolvedStream, StreamUnavailable


def gray(rows):
    return bytes(v for row in rows for v in row)


def test_dhash_encodes_brightness_steps_and_ignores_flat_frames():
    rising = gray([[x * 20 for x in range(9)]] * 8)
    falling = gray([[200 - x * 20 for x in range(9)]] * 8)

    assert dhash(rising) == 0
    assert dhash(falling) == (1 << 64) - 1
    assert dhash(gray([[16] * 9] * 8)) is None  # black frame: no content to compare
    assert dhash(b"\x00" * 10) is None  # truncated output


def test_hamming_counts_differing_bits():
    assert hamming(0b1011, 0b0010) == 2


def test_same_content_needs_two_matching_frames_and_no_clear_mismatch():
    a = [0, 1 << 63, 7]
    assert same_content(a, [1, (1 << 63) | 3, 6]) is True
    assert same_content(a, [0, None, None]) is None  # one comparable frame is not enough
    assert same_content(a, [0, 1 << 63, (1 << 64) - 1]) is False  # one frame clearly differs
    assert same_content(a, [(1 << 64) - 1, (1 << 40) - 1, (1 << 50) - 1]) is False


def test_frame_command_seeks_and_sends_the_stream_headers():
    stream = ResolvedStream(
        url="https://cdn.site.example/v.mp4", kind="file",
        headers={"User-Agent": "UA/1", "Referer": "https://site.example/v/1"},
        cookies=(("sid", "x", "site.example"),), duration=600.0,
    )

    cmd = frame_command("ffmpeg", stream, 150.0)

    assert cmd[cmd.index("-ss") + 1] == "150.0"
    assert cmd[cmd.index("-user_agent") + 1] == "UA/1"
    assert cmd[cmd.index("-headers") + 1] == "Referer: https://site.example/v/1\r\nCookie: sid=x\r\n"
    assert cmd.index("-ss") < cmd.index("-i") and cmd[cmd.index("-i") + 1] == stream.url
    assert "scale=9:8,format=gray" in cmd


def result(n, duration=600.0):
    return SearchResult(url=f"https://site{n}.example/v/{n}", title=f"Video {n}", source="engine", duration=duration)


def make_matcher(hashes_by_url, fail=()):
    resolved = []

    async def resolver(url):
        resolved.append(url)
        if url in fail:
            raise StreamUnavailable("nope")
        return ResolvedStream(url=url, kind="file", headers={}, cookies=(), duration=600.0)

    async def hasher(stream, at):
        return hashes_by_url[stream.url][[0.25, 0.5, 0.75].index(round(at / 600.0, 2))]

    return FrameMatcher(resolver=resolver, hasher=hasher), resolved


@pytest.mark.asyncio
async def test_frame_matcher_compares_frames_at_the_same_points_of_both_videos():
    a, b, c = result(1), result(2), result(3)
    matcher, resolved = make_matcher({
        a.url: [0, 1 << 63, 7], b.url: [1, 1 << 63, 7], c.url: [(1 << 64) - 1, (1 << 40) - 1, (1 << 50) - 1],
    })

    assert await matcher.same_video(a, b) is True
    assert await matcher.same_video(a, c) is False
    assert resolved.count(a.url) == 1  # fingerprints are cached per URL


@pytest.mark.asyncio
async def test_frame_matcher_treats_unplayable_or_untimed_results_as_different():
    a, b = result(1), result(2)
    matcher, _ = make_matcher({a.url: [0, 1, 2]}, fail={b.url})

    assert await matcher.same_video(a, b) is False
    assert await matcher.same_video(a, result(4, duration=None)) is False


@pytest.mark.asyncio
async def test_frames_of_one_stream_are_read_one_at_a_time_at_the_streams_own_length():
    # Some sites allow one connection per stream link; listed lengths can be wrong.
    import asyncio

    active, peak, points = {}, {}, []

    async def resolver(url):
        return ResolvedStream(url=url, kind="file", headers={}, cookies=(), duration=400.0)

    async def hasher(stream, at):
        active[stream.url] = active.get(stream.url, 0) + 1
        peak[stream.url] = max(peak.get(stream.url, 0), active[stream.url])
        await asyncio.sleep(0)
        active[stream.url] -= 1
        points.append(at)
        return 0

    matcher = FrameMatcher(resolver=resolver, hasher=hasher)
    await matcher.same_video(result(1, duration=7000.0), result(2, duration=7001.0))

    assert set(peak.values()) == {1}
    assert sorted(set(points)) == [100.0, 200.0, 300.0]
