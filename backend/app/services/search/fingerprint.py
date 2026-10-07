"""
Tell whether two search results are the same video by what they show.
Each video is sampled at the same points of its length (a quarter, half and
three quarters in) and every frame is reduced to a 64-bit difference hash
(dHash): ffmpeg scales the frame to 9x8 grey pixels and each bit records
whether a pixel is brighter than its right neighbour. Re-encodes, resizes
and watermarks change a few bits, different footage changes about half.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable

import imageio_ffmpeg

from app.services.search.models import SearchResult
from app.services.search.streams import ResolvedStream, StreamUnavailable, resolve_stream

logger = logging.getLogger("VideoSearch")

SAMPLE_POINTS = (0.25, 0.5, 0.75)
MATCH_DISTANCE = 10  # bits; at or under this, two frames show the same picture
MISMATCH_DISTANCE = 20  # bits; over this, they clearly do not
_MIN_FRAME_CONTRAST = 24  # brightness range under this is a blank frame
_FRAME_TIMEOUT_S = 30
_CACHE_SIZE = 256

Fingerprint = list[int | None]
Resolver = Callable[[str], Awaitable[ResolvedStream]]
Hasher = Callable[[ResolvedStream, float], Awaitable[int | None]]


def dhash(pixels: bytes) -> int | None:
    """Difference hash of a 9x8 grey frame, or None for a blank or short frame."""
    if len(pixels) != 72 or max(pixels) - min(pixels) < _MIN_FRAME_CONTRAST:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def same_content(a: Fingerprint, b: Fingerprint) -> bool | None:
    """
    True when at least two frame pairs match and none clearly differs, False
    when any pair clearly differs, None when there is too little to compare.
    """
    distances = [hamming(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if any(d > MISMATCH_DISTANCE for d in distances):
        return False
    if sum(d <= MATCH_DISTANCE for d in distances) >= 2:
        return True
    return None


def frame_command(ffmpeg: str, stream: ResolvedStream, at: float) -> list[str]:
    """ffmpeg arguments that print one 9x8 grey frame at `at` seconds as raw bytes."""
    headers = stream.request_headers(stream.url)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(at)]
    if "User-Agent" in headers:
        cmd += ["-user_agent", headers["User-Agent"]]
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k != "User-Agent")
    if extra:
        cmd += ["-headers", extra]
    return cmd + ["-i", stream.url, "-frames:v", "1", "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-"]


async def frame_hash(stream: ResolvedStream, at: float) -> int | None:
    proc = await asyncio.create_subprocess_exec(
        *frame_command(imageio_ffmpeg.get_ffmpeg_exe(), stream, at),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FRAME_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return None
    return dhash(stdout)


class FrameMatcher:
    """
    Compares results by their frames. Fingerprints are cached per URL; the
    cache is shared by searches running in different threads.
    """

    def __init__(self, resolver: Resolver = resolve_stream, hasher: Hasher = frame_hash) -> None:
        self._resolver = resolver
        self._hasher = hasher
        self._cache: OrderedDict[str, Fingerprint] = OrderedDict()
        self._lock = threading.Lock()

    async def same_video(self, a: SearchResult, b: SearchResult) -> bool:
        if a.duration is None or b.duration is None:
            return False
        first, second = await asyncio.gather(self._fingerprint(a), self._fingerprint(b))
        return same_content(first, second) is True

    async def _fingerprint(self, result: SearchResult) -> Fingerprint:
        with self._lock:
            if result.url in self._cache:
                self._cache.move_to_end(result.url)
                return self._cache[result.url]
        try:
            stream = await self._resolver(result.url)
            # One frame at a time: some stream links allow a single connection.
            # The stream's own length wins over the length the listing showed.
            length = stream.duration or result.duration
            fingerprint = [await self._hasher(stream, round(length * point, 1)) for point in SAMPLE_POINTS]
        except StreamUnavailable as exc:
            logger.info(f"Content check skipped, no stream for {result.url[:100]}: {exc}")
            fingerprint = [None] * len(SAMPLE_POINTS)
        with self._lock:
            self._cache[result.url] = fingerprint
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return fingerprint
