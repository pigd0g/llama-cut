from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .ffmpeg.probe import run_ffprobe


# --- Dataclass --------------------------------------------------------------

@dataclass
class VideoMetadata:
    """Technical metadata extracted from a source video via ffprobe.

    All string fields default to "" so callers never see None. Numeric
    fields default to 0. The ``raw`` field preserves the full ffprobe JSON
    so future expansion does not require a re-probe.
    """

    source_filename: str = ""
    source_path: str = ""
    duration: float = 0.0
    duration_hms: str = ""
    container_format: str = ""

    # Video stream
    video_codec: str = ""
    video_profile: str = ""
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    avg_frame_rate_raw: str = ""
    pixel_format: str = ""
    aspect_ratio: str = ""
    video_bitrate: str = ""
    num_video_streams: int = 0

    # Audio stream (first audio found)
    audio_codec: str = ""
    audio_sample_rate: str = ""
    audio_channels: int = 0
    audio_channel_layout: str = ""
    audio_bitrate: str = ""
    num_audio_streams: int = 0

    # Format-level + stream-level tags merged into one dict.
    tags: dict[str, str] = field(default_factory=dict)

    # Full ffprobe JSON for future expansion.
    raw: dict = field(default_factory=dict)


# --- Extraction -------------------------------------------------------------

def extract_metadata(video_path: str) -> Optional[VideoMetadata]:
    """Run ffprobe against *video_path* and return a populated VideoMetadata.

    Returns ``None`` if ffprobe fails or the result cannot be parsed.
    """
    result = run_ffprobe(video_path)
    if result is None or not result.raw:
        return None
    return parse_metadata(result.raw, str(video_path))


def parse_metadata(data: dict, video_path: str = "") -> VideoMetadata:
    """Build a VideoMetadata from a raw ffprobe JSON dict.

    Does not raise on missing fields — missing values become empty strings
    or zeros so the markdown renderer can handle them gracefully.
    """
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    # --- Duration (format-level) ------------------------------------------
    duration = 0.0
    try:
        duration = float(fmt.get("duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    # --- Video stream (first video found) ---------------------------------
    vs = video_streams[0] if video_streams else {}
    width = _as_int(vs.get("width"))
    height = _as_int(vs.get("height"))
    fps = _parse_fps(vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "0/0")

    # --- Audio stream (first audio found) ---------------------------------
    aus = audio_streams[0] if audio_streams else {}

    # --- Tags (format-level + stream-level, merged) ----------------------
    tags: dict[str, str] = {}
    for k, v in (fmt.get("tags", {}) or {}).items():
        tags[k] = str(v)
    for s in (vs, aus):
        for k, v in (s.get("tags", {}) or {}).items():
            if k not in tags:
                tags[k] = str(v)

    return VideoMetadata(
        source_filename=Path(video_path).name if video_path else "",
        source_path=video_path,
        duration=duration,
        duration_hms=_format_duration_hms(duration),
        container_format=fmt.get("format_name", "") or "",
        video_codec=vs.get("codec_name", "") or "",
        video_profile=vs.get("profile", "") or "",
        width=width,
        height=height,
        frame_rate=fps,
        avg_frame_rate_raw=vs.get("avg_frame_rate", "") or "",
        pixel_format=vs.get("pix_fmt", "") or "",
        aspect_ratio=vs.get("display_aspect_ratio", "") or "",
        video_bitrate=str(vs.get("bit_rate", "") or ""),
        num_video_streams=len(video_streams),
        audio_codec=aus.get("codec_name", "") or "",
        audio_sample_rate=str(aus.get("sample_rate", "") or ""),
        audio_channels=_as_int(aus.get("channels")),
        audio_channel_layout=aus.get("channel_layout", "") or "",
        audio_bitrate=str(aus.get("bit_rate", "") or ""),
        num_audio_streams=len(audio_streams),
        tags=tags,
        raw=data,
    )


# --- Markdown rendering -----------------------------------------------------

def metadata_to_markdown(meta: VideoMetadata) -> str:
    """Render a single VideoMetadata as a Markdown section.

    The heading is ``## <filename>`` so multiple videos can be combined
    under a single ``# Video Metadata`` H1.
    """
    lines: list[str] = []
    lines.append(f"## {meta.source_filename}")
    lines.append("")
    lines.append(f"- Source path: `{meta.source_path}`" if meta.source_path
                 else "- Source path: _(unknown)_")
    lines.append(f"- Duration: {meta.duration_hms}" if meta.duration_hms
                 else "- Duration: _(unknown)_")
    if meta.width and meta.height:
        lines.append(f"- Resolution: {meta.width} × {meta.height}")
    else:
        lines.append("- Resolution: _(unknown)_")
    if meta.frame_rate:
        lines.append(f"- Frame Rate: {_format_fps(meta.frame_rate)} fps")
    else:
        lines.append("- Frame Rate: _(unknown)_")
    if meta.avg_frame_rate_raw:
        lines.append(f"- Average Frame Rate: {meta.avg_frame_rate_raw}")
    codec_line = meta.video_codec
    if meta.video_profile:
        codec_line += f" (profile: {meta.video_profile})"
    lines.append(f"- Codec: {codec_line}" if codec_line else "- Codec: _(unknown)_")
    lines.append(f"- Pixel Format: {meta.pixel_format}" if meta.pixel_format
                 else "- Pixel Format: _(unknown)_")
    lines.append(f"- Aspect Ratio: {meta.aspect_ratio}" if meta.aspect_ratio
                 else "- Aspect Ratio: _(unknown)_")
    if meta.video_bitrate:
        lines.append(f"- Video Bitrate: {_format_bitrate(meta.video_bitrate)}")
    lines.append(f"- Video Streams: {meta.num_video_streams}")
    if meta.container_format:
        lines.append(f"- Container Format: {meta.container_format}")
    # Audio
    if meta.audio_codec:
        lines.append(f"- Audio Codec: {meta.audio_codec}")
        if meta.audio_sample_rate:
            lines.append(f"- Audio Sample Rate: {_format_sample_rate(meta.audio_sample_rate)}")
        lines.append(f"- Audio Channels: {meta.audio_channels}"
                      + (f" ({meta.audio_channel_layout})" if meta.audio_channel_layout else ""))
        if meta.audio_bitrate:
            lines.append(f"- Audio Bitrate: {_format_bitrate(meta.audio_bitrate)}")
    else:
        lines.append("- Audio: _(no audio stream)_")
    lines.append(f"- Audio Streams: {meta.num_audio_streams}")
    # Tags
    if meta.tags:
        lines.append("- Tags:")
        for k in sorted(meta.tags):
            lines.append(f"  - {k}: {meta.tags[k]}")
    return "\n".join(lines)


def metadata_to_markdown_all(metas: list[VideoMetadata]) -> str:
    """Render multiple VideoMetadata as a single ``# Video Metadata`` document.

    Each video is a ``## <filename>`` subsection. An empty list produces
    an empty-string document (no heading) so callers can guard on truthiness.
    """
    if not metas:
        return ""
    parts: list[str] = ["# Video Metadata", ""]
    for meta in metas:
        parts.append(metadata_to_markdown(meta))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# --- Helpers ----------------------------------------------------------------

def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def _format_duration_hms(seconds: float) -> str:
    """Return HH:MM:SS (no fractional seconds)."""
    if seconds <= 0:
        return "00:00:00"
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_fps(fps: float) -> str:
    """Render fps with up to 2 decimal places, dropping trailing zeros."""
    if fps <= 0:
        return "0"
    s = f"{fps:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _format_bitrate(bitrate: str) -> str:
    """Render a raw bitrate string (in bits/s) as a human-readable value."""
    try:
        bps = float(bitrate)
    except (TypeError, ValueError):
        return bitrate
    if bps <= 0:
        return bitrate
    if bps >= 1_000_000:
        val = f"{bps / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{val} Mbps"
    if bps >= 1_000:
        val = f"{bps / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{val} kbps"
    return f"{int(bps)} bps"


def _format_sample_rate(rate: str) -> str:
    """Render a sample rate like '48000' as '48 kHz'."""
    try:
        hz = int(rate)
    except (TypeError, ValueError):
        return rate
    if hz <= 0:
        return rate
    if hz >= 1000:
        return f"{hz / 1000:.0f} kHz"
    return f"{hz} Hz"