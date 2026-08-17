from __future__ import annotations

import re


def format_timestamp(pts_time: float) -> str:
    """Convert pts_time in seconds to HH-MM-SS-mmm (zero-padded)."""
    if pts_time < 0:
        pts_time = 0.0
    total_ms = int(round(pts_time * 1000.0))
    hours = total_ms // 3_600_000
    remaining = total_ms % 3_600_000
    minutes = remaining // 60_000
    remaining %= 60_000
    seconds = remaining // 1000
    ms = remaining % 1000
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}-{ms:03d}"


def build_final_filename(video_stem: str, pts_time: float, frame_index: int) -> str:
    """Build the final frame filename: {stem}-{HH-MM-SS-mmm}-{NNNN}.jpg"""
    ts = format_timestamp(pts_time)
    return f"{video_stem}-{ts}-{frame_index:04d}.jpg"


_SHOWINFO_RE = re.compile(
    r"n:\s*(?P<n>\d+).*?pts_time:\s*(?P<t>[\d.]+)"
)


def parse_showinfo_stderr(stderr_text: str) -> list[float]:
    """Extract a list of pts_time values (in seconds) from ffmpeg showinfo output.

    The showinfo filter emits lines like:
      [Parsed_showinfo_0 @ 0x...]   n:   1 ...
    We return pts_time in the order the frames were written.
    """
    times: list[float] = []
    for line in stderr_text.splitlines():
        m = _SHOWINFO_RE.search(line)
        if not m:
            continue
        try:
            t = float(m.group("t"))
        except ValueError:
            continue
        times.append(max(0.0, t))
    return times


def even_subsample_indices(total: int, target: int) -> list[int]:
    """Return a deterministic, evenly-spaced subset of indices [0, total).

    Preserves the first and last index when possible. Always returns indices
    in ascending order. Deterministic for the same (total, target).
    """
    if total <= 0 or target <= 0:
        return []
    if total <= target:
        return list(range(total))
    if target == 1:
        return [total // 2]
    step = total / target
    idxs: list[int] = []
    seen: set[int] = set()
    for i in range(target):
        v = int(round(i * step))
        if v >= total:
            v = total - 1
        if v not in seen:
            seen.add(v)
            idxs.append(v)
    idxs.sort()
    return idxs