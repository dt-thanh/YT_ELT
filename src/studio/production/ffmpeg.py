"""
The one place that runs FFmpeg. Finds the binary, runs it with a timeout, and reads
back what a file really is (QC, spec §10.12) without needing ffprobe: the pilot uses
the static binary from `imageio-ffmpeg` (extra "media"), which ships ffmpeg only.

Order of lookup: $FFMPEG_BIN, `ffmpeg` on PATH, imageio-ffmpeg's bundled binary.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def ffmpeg_bin() -> str | None:
    env = os.environ.get("FFMPEG_BIN")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def run(args: list[str], *, cwd: Path | str | None = None, timeout: int = 1800) -> str:
    """Run ffmpeg with `args`; returns stderr (where ffmpeg writes its report)."""
    exe = ffmpeg_bin()
    if exe is None:
        raise FFmpegError("ffmpeg not found: install it, or `pip install -e .[media]`")
    proc = subprocess.run([exe, "-hide_banner", "-nostdin", "-y", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
    return proc.stderr


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int | None
    height: int | None
    has_audio: bool


_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\d+:\d+.*?: Video: .*?(\d{2,5})x(\d{2,5})")


def probe(path: Path | str) -> MediaInfo:
    exe = ffmpeg_bin()
    if exe is None:
        raise FFmpegError("ffmpeg not found")
    # `ffmpeg -i file` with no output exits 1 but prints the container report.
    err = subprocess.run([exe, "-hide_banner", "-i", str(path)], capture_output=True,
                         text=True, timeout=60).stderr
    d = _DURATION.search(err)
    if not d:
        raise FFmpegError(f"cannot read {path}: {err.strip().splitlines()[-1:]}")
    v = _VIDEO.search(err)
    return MediaInfo(duration=int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]),
                     width=int(v[1]) if v else None, height=int(v[2]) if v else None,
                     has_audio=" Audio: " in err)


_INTEGRATED = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?) LUFS")


def loudness_lufs(path: Path | str) -> float:
    """Integrated loudness (EBU R128) of the file's audio."""
    err = run(["-i", str(path), "-map", "0:a", "-af", "ebur128", "-f", "null", "-"])
    m = _INTEGRATED.search(err)
    if not m:
        raise FFmpegError("no ebur128 summary")
    return float(m[1])
