import logging
import os
import re
import subprocess

import imageio_ffmpeg

logger = logging.getLogger("VideoUtils")


def probe_video_dimensions(video_path: str) -> tuple[int, int] | None:
    """Return (width, height) via ffmpeg -i probe, or None if unable."""
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg_exe, "-i", video_path],
            capture_output=True, text=True,
        )
        match = re.search(r'Video:.*?(\d{2,5})x(\d{2,5})', result.stderr)
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return None


def ensure_min_quality(video_path: str, min_height: int = 720) -> str:
    """
    Resize video to exactly min_height using Lanczos + CRF 18 (lossless-quality).
    Upscales if below, downscales if above. Returns the final file path.
    """
    dims = probe_video_dimensions(video_path)
    if dims is None:
        logger.warning(f"Could not probe {os.path.basename(video_path)} — skipping resize")
        return video_path

    width, height = dims
    if height == min_height:
        logger.info(f"Video {width}x{height} — already at {min_height}p")
        return video_path

    direction = "upscaling" if height < min_height else "downscaling"
    logger.info(f"Video is {width}x{height} — {direction} to {min_height}p with Lanczos")
    base, ext = os.path.splitext(video_path)
    out_path = f"{base}_{min_height}p{ext}"
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-y",
            "-i", video_path,
            "-vf", f"scale=-2:{min_height}:flags=lanczos",
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "slow",
            "-c:a", "copy",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            os.remove(video_path)
            size_kb = os.path.getsize(out_path) // 1024
            logger.info(f"Resized to {min_height}p: {os.path.basename(out_path)} ({size_kb}KB)")
            return out_path
        logger.error(f"Resize failed (rc={result.returncode}): {result.stderr[-300:]}")
    except subprocess.TimeoutExpired:
        logger.error(f"Resize timed out for {os.path.basename(video_path)}")
    except Exception as e:
        logger.error(f"Resize error: {e}")
    return video_path
