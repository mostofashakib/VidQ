import os
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import imageio_ffmpeg


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


def probe_duration(path: str) -> Optional[float]:
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run([ffmpeg_exe, "-i", path], capture_output=True, text=True)
        match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = float(match.group(3))
            return hours * 3600 + minutes * 60 + seconds
    except Exception:
        pass
    return None


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
