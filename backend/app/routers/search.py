import logging
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.routers.auth import verify_token
from app.services.search.embed import discover_embed
from app.services.search.fingerprint import FrameMatcher
from app.services.search.playback import (
    StreamEntry,
    StreamRegistry,
    UpstreamRefused,
    is_playlist,
    open_upstream,
    rewrite_playlist,
    stream_reachable,
)
from app.services.search.streams import StreamUnavailable, resolve_stream
from app.services.url_safety import is_safe_url

logger = logging.getLogger("VideoSearch")

router = APIRouter()

MAX_DESCRIPTION_CHARS = 500
_RELAYED_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")
_store = None
_streams = StreamRegistry()
_resolve_stream = resolve_stream
_matcher = FrameMatcher()


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0))


def _get_store():
    """The process-wide search store, built on first use."""
    global _store
    if _store is None:
        from app.config import get_settings
        from app.services.search.agent import SearchAgent
        from app.services.search.sources import default_sources
        from app.services.search.store import SearchStore
        from app.state import llm_manager

        open_sources = default_sources(get_settings())
        _store = SearchStore(agent_factory=lambda: SearchAgent(llm_manager, open_sources, matcher=_matcher))
    return _store


@router.post("/search")
def start_search(data: dict = Body(...), token: str = Depends(verify_token)):
    """Start an agentic video search for a description. Poll GET /search/{id}."""
    description = str(data.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="Describe the video you want to find.")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise HTTPException(status_code=400, detail=f"Keep the description under {MAX_DESCRIPTION_CHARS} characters.")
    return _get_store().start(description).snapshot()


@router.get("/search/{search_id}")
def get_search(search_id: str, token: str = Depends(verify_token)):
    session = _get_store().get(search_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Search not found or expired.")
    return session.snapshot()


@router.post("/search/{search_id}/more")
def more_results(search_id: str, token: str = Depends(verify_token)):
    """Fetch the next results. Poll GET /search/{id} until it is done."""
    from app.services.search.store import SearchBusy

    try:
        return _get_store().more(search_id).snapshot()
    except KeyError:
        raise HTTPException(status_code=404, detail="Search not found or expired.")
    except SearchBusy:
        raise HTTPException(status_code=409, detail="This search is still running.")


# ── Playback ──────────────────────────────────────────────────────────────────
# The stream routes take no login token because a <video> element cannot send
# one. The stream ID is the key: it is random, expires, and only the client
# that called POST /search/play receives it.

@router.post("/search/play")
async def play_result(data: dict = Body(...), token: str = Depends(verify_token)):
    """
    How the app plays a search result: its stream relayed through the backend
    ({"mode": "stream", ...}), or else the site's own oEmbed player
    ({"mode": "embed", "embed_url": ...}).
    """
    url = str(data.get("url") or "").strip()
    if not is_safe_url(url):
        raise HTTPException(status_code=400, detail="This link cannot be played.")
    reason = "the site refused the video stream"
    try:
        stream = await _resolve_stream(url)
    except StreamUnavailable as exc:
        stream, reason = None, str(exc)
    async with _http_client() as client:
        if stream is not None and await stream_reachable(client, stream):
            stream_id = _streams.register(stream)
            return {"mode": "stream", "stream_id": stream_id, "kind": stream.kind, "path": f"/search/stream/{stream_id}"}
        embed_url = await discover_embed(client, url)
    if embed_url:
        return {"mode": "embed", "embed_url": embed_url}
    raise HTTPException(status_code=422, detail=f"This video cannot be played here: {reason}")


def _entry(stream_id: str) -> StreamEntry:
    entry = _streams.get(stream_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Stream not found or expired.")
    return entry


@router.get("/search/stream/{stream_id}")
async def relay_stream(stream_id: str, request: Request):
    entry = _entry(stream_id)
    return await _relay(entry, stream_id, entry.stream.url, request)


@router.get("/search/stream/{stream_id}/part")
async def relay_stream_part(stream_id: str, u: str, request: Request):
    """A segment, key or sub-playlist listed in a playlist this stream served."""
    entry = _entry(stream_id)
    if not entry.allows(u):
        raise HTTPException(status_code=403, detail="Not part of this stream.")
    return await _relay(entry, stream_id, u, request)


async def _relay(entry: StreamEntry, stream_id: str, url: str, request: Request):
    """Fetch url with the site's headers and pass it on, with ranges for seeking."""
    headers = entry.stream.request_headers(url)
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    client = _http_client()
    try:
        upstream = await open_upstream(client, url, headers)
    except (UpstreamRefused, httpx.HTTPError) as exc:
        await client.aclose()
        logger.warning(f"Stream relay failed for {url[:100]}: {exc}")
        raise HTTPException(status_code=502, detail="The video site could not be reached.")
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"The video site answered {upstream.status_code}.")

    content_type = upstream.headers.get("content-type", "")
    if is_playlist(str(upstream.url), content_type):
        try:
            body = (await upstream.aread()).decode(errors="replace")
        finally:
            await upstream.aclose()
            await client.aclose()
        part = f"/search/stream/{stream_id}/part?u="
        text, urls = rewrite_playlist(body, str(upstream.url), lambda u: part + quote(u, safe=""))
        entry.allow(urls)
        return Response(text, media_type="application/vnd.apple.mpegurl")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    relayed = {k: v for k, v in upstream.headers.items() if k.lower() in _RELAYED_HEADERS}
    return StreamingResponse(body(), status_code=upstream.status_code, headers=relayed)
