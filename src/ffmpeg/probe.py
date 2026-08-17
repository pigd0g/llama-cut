from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ProbeResult:
    duration: float
    width: int
    height: int
    codec: str
    fps: float
    raw: dict


def _ffprobe_bin() -> str:
    p = shutil.which("ffprobe")
    return p if p else "ffprobe"


def run_ffprobe(video_path: str | Path) -> Optional[ProbeResult]:
    """Run ffprobe and return parsed result. Returns None on failure."""
    cmd = [
        _ffprobe_bin(),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return parse_ffprobe_json(proc.stdout, str(video_path))


def parse_ffprobe_json(stdout: str, video_path: str = "") -> Optional[ProbeResult]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return parse_probe_data(data, video_path)


def parse_probe_data(data: dict, video_path: str = "") -> Optional[ProbeResult]:
    fmt = data.get("format", {})
    duration = 0.0
    try:
        duration = float(fmt.get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0

    width = height = 0
    codec = ""
    fps = 0.0
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        try:
            width = int(stream.get("width", 0))
            height = int(stream.get("height", 0))
        except (TypeError, ValueError):
            pass
        codec = stream.get("codec_name", "") or ""
        fps = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0")
        break

    return ProbeResult(
        duration=duration,
        width=width,
        height=height,
        codec=codec,
        fps=fps,
        raw=data,
    )


def _parse_fps(expr: str) -> float:
    """Parse expressions like '30000/1001' or '25/1'."""
    if not expr or expr == "0/0":
        return 0.0
    try:
        if "/" in expr:
            num, den = expr.split("/", 1)
            n, d = float(num), float(den)
            return n / d if d else 0.0
        return float(expr)
    except (ValueError, ZeroDivisionError):
        return 0.0