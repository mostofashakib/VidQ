"""Tests for the search session store, default sources and the /search routes."""

import pytest

from app.services.search import sources as sources_module
from app.services.search.agent import SearchFailed
from app.services.search.models import SearchResult
from app.services.search.store import SearchBusy, SearchStore

AUTH = {"Authorization": "Bearer test-token"}


def r(n):
    return SearchResult(url=f"https://v.example/{n}", title=f"Video {n}", source="youtube")


class FakeAgent:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def first_page(self, session):
        self.calls.append("first")
        session.phase = "searching"
        if self.error:
            raise self.error
        session.queries = ["q"]
        session.results.extend(r(n) for n in range(5))

    async def more(self, session):
        self.calls.append("more")
        session.results.extend(r(n) for n in range(5, 10))
        session.exhausted = True


def run_now(target):
    target()


def make_store(agent, **kwargs):
    return SearchStore(agent_factory=lambda: agent, run_in_background=run_now, **kwargs)


# ── Store ─────────────────────────────────────────────────────────────────────

def test_store_runs_the_first_page_and_marks_the_session_done():
    store = make_store(FakeAgent())

    session = store.start("deep sea")

    assert store.get(session.search_id) is session
    assert session.status == "done"
    assert len(session.results) == 5


def test_store_records_expected_and_unexpected_failures():
    failed = make_store(FakeAgent(SearchFailed("Every search source failed: x"))).start("deep sea")
    crashed = make_store(FakeAgent(RuntimeError("boom"))).start("deep sea")

    assert (failed.status, failed.error) == ("failed", "Every search source failed: x")
    assert (crashed.status, crashed.error) == ("failed", "Search failed unexpectedly. Check the backend log.")
    assert failed.phase is None and crashed.phase is None


def test_more_refuses_a_running_search_and_unknown_ids():
    agent = FakeAgent()
    store = make_store(agent)
    session = store.start("deep sea")

    session.status = "running"
    with pytest.raises(SearchBusy):
        store.more(session.search_id)
    with pytest.raises(KeyError):
        store.more("missing")

    session.status = "done"
    store.more(session.search_id)
    assert agent.calls == ["first", "more"]
    assert len(session.results) == 10


def test_expired_sessions_are_dropped(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.services.search.store.time.time", lambda: clock[0])
    store = make_store(FakeAgent(), ttl_s=60)
    old = store.start("old")

    clock[0] += 61
    store.start("new")

    assert store.get(old.search_id) is None


# ── Default sources ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_sources_fall_back_to_ytdlp_when_the_browser_fails(monkeypatch):
    class BrokenFetcher:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            raise RuntimeError("no browser")

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr(sources_module, "BrowserPageFetcher", BrokenFetcher)

    async with sources_module.default_sources(object())() as sources:
        assert [s.name for s in sources] == ["youtube", "bilibili"]


@pytest.mark.asyncio
async def test_default_sources_add_both_engines_with_the_safe_search_setting(monkeypatch):
    class Fetcher:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    monkeypatch.setattr(sources_module, "BrowserPageFetcher", Fetcher)
    settings = type("S", (), {"search_safe_search": "off"})()

    async with sources_module.default_sources(settings)() as sources:
        assert [s.name for s in sources] == ["youtube", "bilibili", "duckduckgo", "brave"]


# ── Routes ────────────────────────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    store = make_store(FakeAgent())
    monkeypatch.setattr("app.routers.search._get_store", lambda: store)
    return store


def test_post_search_starts_a_search_and_get_reports_it(client, store):
    started = client.post("/search", json={"description": "  deep sea creatures  "}, headers=AUTH)

    assert started.status_code == 200
    search_id = started.json()["search_id"]
    status = client.get(f"/search/{search_id}", headers=AUTH).json()
    assert status["description"] == "deep sea creatures"
    assert status["status"] == "done"
    assert [x["title"] for x in status["results"]] == [f"Video {n}" for n in range(5)]


@pytest.mark.parametrize("description", ["", "   ", "x" * 501])
def test_post_search_rejects_empty_or_overlong_descriptions(client, store, description):
    assert client.post("/search", json={"description": description}, headers=AUTH).status_code == 400


def test_more_returns_the_next_results_and_404s_unknown_searches(client, store):
    search_id = client.post("/search", json={"description": "deep sea"}, headers=AUTH).json()["search_id"]

    more = client.post(f"/search/{search_id}/more", headers=AUTH)

    assert more.status_code == 200
    assert len(more.json()["results"]) == 10
    assert more.json()["has_more"] is False
    assert client.get("/search/missing", headers=AUTH).status_code == 404
    assert client.post("/search/missing/more", headers=AUTH).status_code == 404


def test_more_on_a_running_search_is_a_conflict(client, store):
    session = store.start("deep sea")
    session.status = "running"

    assert client.post(f"/search/{session.search_id}/more", headers=AUTH).status_code == 409


# ── Playback ──────────────────────────────────────────────────────────────────

import httpx  # noqa: E402

from app.services.search.playback import StreamRegistry  # noqa: E402
from app.services.search.streams import ResolvedStream, StreamUnavailable  # noqa: E402

PLAYLIST = "#EXTM3U\n#EXTINF:4.0,\nseg1.ts\n"


def upstream(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v.mp4":
        assert request.headers["referer"] == "https://site.example/v/1"
        if request.headers.get("range") == "bytes=0-3":
            return httpx.Response(206, content=b"abcd", headers={
                "content-type": "video/mp4", "content-range": "bytes 0-3/10", "accept-ranges": "bytes",
            })
        return httpx.Response(200, content=b"abcdefghij", headers={"content-type": "video/mp4"})
    if path == "/hls/index.m3u8":
        return httpx.Response(200, text=PLAYLIST, headers={"content-type": "application/vnd.apple.mpegurl"})
    if path == "/hls/seg1.ts":
        return httpx.Response(200, content=b"TS", headers={"content-type": "video/mp2t"})
    if path == "/to-internal":
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    if path == "/forbidden.mp4":
        return httpx.Response(403)
    if path == "/v/embeddable":
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            '<link rel="alternate" type="application/json+oembed" href="https://site.example/oembed">'))
    if path == "/oembed":
        return httpx.Response(200, json={"html": '<iframe src="https://site.example/embed/7"></iframe>'})
    return httpx.Response(404)


@pytest.fixture
def playback(monkeypatch):
    registry = StreamRegistry()
    resolved = {}

    async def resolver(url):
        if url not in resolved:
            raise StreamUnavailable("Unsupported URL")
        return resolved[url]

    monkeypatch.setattr("app.routers.search._streams", registry)
    monkeypatch.setattr("app.routers.search._resolve_stream", resolver)
    monkeypatch.setattr("app.routers.search._http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))

    def add(page_url, stream_url, kind="file"):
        resolved[page_url] = ResolvedStream(
            url=stream_url, kind=kind, headers={"Referer": "https://site.example/v/1"}, cookies=(), duration=10.0,
        )
        return resolved[page_url]

    add.registry = registry
    return add


def test_play_resolves_a_result_and_relays_its_stream_with_ranges(client, playback):
    playback("https://site.example/v/1", "https://cdn.site.example/v.mp4")

    started = client.post("/search/play", json={"url": "https://site.example/v/1"}, headers=AUTH)

    assert started.status_code == 200
    body = started.json()
    assert body["mode"] == "stream" and body["kind"] == "file"
    assert body["path"] == f"/search/stream/{body['stream_id']}"
    whole = client.get(body["path"])
    part = client.get(body["path"], headers={"Range": "bytes=0-3"})
    assert (whole.status_code, whole.content) == (200, b"abcdefghij")
    assert (part.status_code, part.content, part.headers["content-range"]) == (206, b"abcd", "bytes 0-3/10")


def test_play_rewrites_hls_playlists_and_relays_only_listed_parts(client, playback):
    playback("https://site.example/v/2", "https://cdn.site.example/hls/index.m3u8", kind="hls")
    path = client.post("/search/play", json={"url": "https://site.example/v/2"}, headers=AUTH).json()["path"]

    playlist = client.get(path)
    segment_link = playlist.text.splitlines()[-1]

    assert playlist.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert segment_link == f"{path}/part?u=https%3A%2F%2Fcdn.site.example%2Fhls%2Fseg1.ts"
    assert client.get(segment_link).content == b"TS"
    unlisted = client.get(f"{path}/part", params={"u": "https://cdn.site.example/hls/other.ts"})
    assert unlisted.status_code == 403


def test_play_refuses_unsafe_or_unplayable_pages_and_unknown_streams(client, playback):
    assert client.post("/search/play", json={"url": "http://127.0.0.1/x"}, headers=AUTH).status_code == 400
    unplayable = client.post("/search/play", json={"url": "https://site.example/v/9"}, headers=AUTH)
    assert unplayable.status_code == 422
    assert "Unsupported URL" in unplayable.json()["detail"]
    assert client.get("/search/stream/missing").status_code == 404


def test_stream_relay_never_follows_a_redirect_to_an_internal_host(client, playback):
    stream_id = playback.registry.register(
        playback("https://site.example/v/3", "https://cdn.site.example/to-internal"))

    assert client.get(f"/search/stream/{stream_id}").status_code == 502


@pytest.mark.parametrize("stream_url", ["https://cdn.site.example/forbidden.mp4", None])
def test_play_falls_back_to_the_sites_own_player_when_the_stream_is_refused(client, playback, stream_url):
    if stream_url:
        playback("https://site.example/v/embeddable", stream_url)

    started = client.post("/search/play", json={"url": "https://site.example/v/embeddable"}, headers=AUTH)

    assert started.status_code == 200
    assert started.json() == {"mode": "embed", "embed_url": "https://site.example/embed/7"}
