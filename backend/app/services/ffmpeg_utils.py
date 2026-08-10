import os
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import imageio_ffmpeg


# Canonical media format every pipeline (convert + combine) re-encodes to.
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_PIX_FMT = "yuv420p"
TARGET_FPS = 30
TARGET_SAMPLE_RATE = 48000
TARGET_CHANNEL_LAYOUT = "stereo"


@dataclass
class ProcessRunResult:
    returncode: int
    stderr: str
    cancelled: bool


def parse_ffmpeg_time(time_value: str) -> float:
    parts = time_value.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid ffmpeg time: {time_value}")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def _probe_output(path: str) -> str:
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    return subprocess.run([ffmpeg_exe, "-i", path], capture_output=True, text=True).stderr


def probe_duration(path: str) -> Optional[float]:
    try:
        match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", _probe_output(path))
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = float(match.group(3))
            return hours * 3600 + minutes * 60 + seconds
    except Exception:
        pass
    return None


def probe_missing_audio(path: str) -> bool:
    """True only when the header parses cleanly and shows no audio stream at all.

    An unreadable header answers False: assuming audio is present and leaving the real
    track alone is safer than replacing it with silence on a probe hiccup.
    """
    try:
        lines = _probe_output(path).splitlines()
    except Exception:
        return False
    if not any(": Video:" in line for line in lines):
        return False
    return not any(": Audio:" in line for line in lines)


def scale_to_target_chain(
    *,
    fps: Optional[int] = None,
    pad_color: str = "black",
    clone_last_frame: bool = False,
) -> str:
    """Filter chain that forces any source to the canonical 1280×720 yuv420p geometry.

    `clone_last_frame` holds the final frame indefinitely; it only makes sense with an
    output duration limit (`-t`), which is what trims it back to a finite length.
    """
    chain = [
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={pad_color}",
        "setsar=1",
        f"format={TARGET_PIX_FMT}",
    ]
    if fps:
        chain.append(f"fps={fps}")
    if clone_last_frame:
        chain.append("tpad=stop=-1:stop_mode=clone")
    return ",".join(chain)


def build_normalize_command(
    *,
    input_path: str,
    output_path: str,
    fps: Optional[int] = None,
    crf: int = 18,
    preset: str = "slow",
    audio_bitrate: str = "192k",
    silent_audio: bool = False,
    fixed_duration: Optional[float] = None,
) -> list[str]:
    """
    ffmpeg command that re-encodes any source to H.264/AAC 1280×720 MP4, letterboxed
    to preserve aspect ratio.

    `silent_audio` bolts on a silent track for sources that have none. `fixed_duration`
    forces both streams to exactly that many seconds — padding a short audio track with
    silence and a short video track with its last frame — so downstream filter graphs
    get clips whose real length matches the duration they were planned against.
    (`-shortest` is not used for this: it overshoots by however far the audio encoder
    has run ahead of the video.)
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-y", "-i", input_path]
    if silent_audio:
        cmd.extend([
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout={TARGET_CHANNEL_LAYOUT}:sample_rate={TARGET_SAMPLE_RATE}",
            "-map", "0:v:0", "-map", "1:a:0",
        ])
    if fixed_duration:
        if not silent_audio:
            cmd.extend(["-af", "apad"])
        cmd.extend(["-t", f"{fixed_duration:.3f}"])
    elif silent_audio:
        cmd.append("-shortest")
    cmd.extend([
        "-vf", scale_to_target_chain(fps=fps, clone_last_frame=fixed_duration is not None),
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-ar", str(TARGET_SAMPLE_RATE), "-ac", "2",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        output_path,
    ])
    return cmd


def run_progress_process(
    *,
    cmd: list[str],
    job,
    lock: threading.Lock,
    popen: Callable[..., subprocess.Popen],
    on_progress: Callable[[float], None] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> ProcessRunResult:
    stderr_lines: list[str] = []
    process_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if cwd is not None:
        process_kwargs["cwd"] = cwd
    if env is not None:
        process_kwargs["env"] = env

    proc = popen(
        cmd,
        **process_kwargs,
    )
    with lock:
        if hasattr(job, "_procs"):
            job._procs.add(proc)
        job._proc = proc

    def drain_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    try:
        for line in proc.stdout:
            line = line.strip()
            if on_progress and line.startswith("out_time="):
                try:
                    on_progress(parse_ffmpeg_time(line[len("out_time="):]))
                except Exception:
                    pass
    except Exception:
        pass

    proc.wait()
    stderr_thread.join(timeout=2)

    with lock:
        if hasattr(job, "_procs"):
            job._procs.discard(proc)
            job._proc = next(iter(job._procs), None)
        else:
            job._proc = None
        cancelled = job.status == "cancelled"

    return ProcessRunResult(
        returncode=proc.returncode,
        stderr="".join(stderr_lines),
        cancelled=cancelled,
    )


def output_file_is_valid(path: str, min_size: int = 1000) -> bool:
    return os.path.exists(path) and os.path.getsize(path) >= min_size
