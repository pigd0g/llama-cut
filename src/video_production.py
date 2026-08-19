"""Stage 8 — Final Video Production engine.

Pure-logic module containing:
  * Ollama configuration (reuses OLLAMA_WORKFLOW_MODEL)
  * Pydantic edit-plan models with storyboard→source traceability
  * ToolRegistry implementing 9 domain-specific FFmpeg tools
  * Multi-turn agent loop using Ollama tool calling
  * Persistence (edit plan, tool log, clear)

The LLM decides *what* should happen. Python decides *how* to safely
execute it. FFmpeg performs the actual media operations.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError


# --- Constants ---------------------------------------------------------------

MAX_AGENT_ROUND_TRIPS = 50

VIDEO_DIR = "video"
CLIPS_SUBDIR = "clips"
OUTPUT_SUBDIR = "output"
PREVIEW_SUBDIR = "preview"
EDIT_PLAN_FILENAME = "edit_plan.json"
TOOL_LOG_FILENAME = "tool_log.json"

# Whitelisted transition types (xfade filter names)
SUPPORTED_TRANSITIONS = frozenset({
    "cut", "dissolve", "fadeblack", "fadewhite", "fadegrays",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circleopen", "circleclose", "zoomin",
})

# Whitelisted render presets
SUPPORTED_PRESETS = frozenset({
    "preview", "youtube_1080p", "youtube_4k", "high_quality",
})

PRESET_PROFILES: dict[str, dict] = {
    "preview": {
        "scale": "1280:-2",
        "vcodec": "libx264",
        "crf": "28",
        "preset": "ultrafast",
        "extra": ["-profile:v", "baseline", "-level", "3.0"],
        "abitrate": "96k",
    },
    "youtube_1080p": {
        "scale": "1920:1080",
        "vcodec": "libx264",
        "crf": "23",
        "preset": "slow",
        "extra": ["-profile:v", "high", "-level", "4.0"],
        "abitrate": "128k",
        "pix_fmt": "yuv420p",
        "faststart": True,
    },
    "youtube_4k": {
        "scale": "3840:2160",
        "vcodec": "libx264",
        "crf": "23",
        "preset": "slow",
        "extra": ["-profile:v", "high", "-level", "5.1"],
        "abitrate": "192k",
        "pix_fmt": "yuv420p",
        "faststart": True,
    },
    "high_quality": {
        "scale": "1920:1080",
        "vcodec": "libx264",
        "crf": "18",
        "preset": "veryslow",
        "extra": ["-profile:v", "high", "-level", "4.2"],
        "abitrate": "192k",
        "pix_fmt": "yuv420p",
        "faststart": True,
    },
}

_NVENC_AVAILABLE: bool | None = None


def _ffmpeg_bin() -> str:
    p = shutil.which("ffmpeg")
    return p if p else "ffmpeg"


def _ffprobe_bin() -> str:
    p = shutil.which("ffprobe")
    return p if p else "ffprobe"


def is_nvenc_available() -> bool:
    """Check whether h264_nvenc is available. Result is cached."""
    global _NVENC_AVAILABLE
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE
    try:
        proc = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        _NVENC_AVAILABLE = "h264_nvenc" in proc.stdout
    except Exception:
        _NVENC_AVAILABLE = False
    return _NVENC_AVAILABLE


# --- System prompt ------------------------------------------------------------

EDITING_SYSTEM_PROMPT = """\
You are an expert video editor and director. Your task is to produce a final \
edited video from the provided storyboard, source footage, and context.

You have access to a set of controlled tools that run FFmpeg operations. \
You do NOT write raw FFmpeg commands. Instead, you call tools with structured \
arguments and the Python layer safely executes them.

## Editing Workflow

Follow these phases in order:

Phase A — Understand:
  Call probe_video() for every source video referenced in the storyboard. \
Review the returned metadata (duration, resolution, fps, codec, audio).

Phase B — Plan:
  Based on the storyboard and probed metadata, mentally create an edit plan \
mapping each storyboard shot to a source clip with start/end timestamps.

Phase C — Build intermediates:
  Call extract_clip() for each shot to create intermediate clip files.

Phase D — Process:
  If the storyboard requires speed changes, scaling, cropping, or color \
adjustment, call create_edit() on the relevant clips. If transitions are \
needed between clips, call create_transition().

Phase E — Mix audio:
  If the storyboard specifies audio adjustments (volume, fades, \
normalization), call mix_audio().

Phase F — Assemble:
  Call assemble_timeline() with the list of clips and transitions to build \
the final video timeline.

Phase G — Render:
  Call render_video() with the assembled timeline and an appropriate preset. \
Use "preview" for a fast preview render, or "youtube_10800p" / "high_quality" \
for a final render.

Phase H — Verify:
  Call validate_video() on the rendered output. If validation fails, read \
the error, correct the issue, and re-render.

## Rules

- Never invent footage. Only use source videos that were probed.
- If the storyboard references footage that cannot be found, report the problem.
- If something cannot be implemented, report it rather than silently skipping it.
- Always probe a video before extracting clips from it.
- Verify timestamps are within the video duration before extracting.
- Use unique output_name values for every intermediate clip.
- Source video files are read-only — never attempt to modify them.
- Do not write files outside the project working directory.
- If a tool returns an error, read the error message and correct your approach.
- Source footage usage must be tracked by source file and time range, not just by filename.
- When selecting a new clip, inspect the existing timeline for previously used ranges from the same source.
- A source file being different does not make footage unique; uniqueness is determined by the source file plus its timestamp range.

### Timeline Integrity

- Never use the same source footage twice unless the storyboard explicitly requires the repetition.
- Never create two timeline shots whose source time ranges overlap, unless the storyboard explicitly requires the overlap.
- Before adding a shot to the timeline, compare its source file and source_start/source_end against every existing timeline shot.
- If a new shot overlaps an existing shot from the same source, either adjust the new range, reuse the existing shot, or report the conflict. Do not silently create the overlap.
- Reusing the same source video at different, non-overlapping timestamps is allowed.
- A longer shot must not contain footage that has already been used by an earlier shot unless that repetition is explicitly intentional.
- Treat the structured timeline as the source of truth. The final edit summary must describe the actual timeline and must not contain shots, transitions, durations, or creative decisions that are not represented in the timeline.
- Every timeline shot must correspond to a specific storyboard shot or an explicitly justified editorial insertion.
- Preserve the storyboard shot identifier in `storyboard_shot` for every timeline shot. Never leave `storyboard_shot` empty when the shot originated from the storyboard.
- Do not create multiple timeline shots for the same storyboard shot unless the storyboard explicitly requires it or the split is necessary to implement the storyboard.
- Every storyboard shot that is required by the storyboard must either be represented in the timeline or be explicitly reported as not implemented.
- Do not silently omit storyboard shots.
- Do not silently duplicate storyboard shots.
- Before finalising the timeline, perform a complete validation pass for duplicate footage, overlapping source ranges, missing storyboard shots, and invalid timeline references.

### Transition Integrity

- Every transition must reference valid adjacent timeline shots.
- A transition must identify the correct preceding and following shots; never attach multiple unrelated transitions to the same shot.
- Do not create transitions that are not represented in the structured timeline.
- The transition definitions and the final edit summary must agree with the actual timeline.
- Validate transition ordering and placement before finalising the edit.



## Output

When you have finished rendering and validating the video, respond with a \
summary of the final video (duration, resolution, file path, and any notes \
about creative decisions made during editing).
"""


REFINEMENT_INSTRUCTIONS = """\
You are refining an existing video edit based on user feedback.

The user has provided feedback on the current edit. Apply the requested \
changes while preserving good decisions from the existing edit.

Use the supplied storyboard, context, and current edit plan to make \
targeted modifications. You do not need to rebuild the entire edit from \
scratch — modify only what the user has requested.

Follow the same phased workflow (probe → extract → edit → assemble → \
render → validate) for any new or modified clips. Reuse existing \
intermediate clips that are not affected by the changes.
"""


# --- Pydantic edit-plan models (per spec §20) --------------------------------

class TimelineItem(BaseModel):
    """A single shot in the edit timeline."""
    id: str = Field(..., description="Unique shot identifier, e.g. 'shot_01'")
    source: str = Field(..., description="Source video filename")
    source_start: float = Field(..., ge=0, description="Start time in seconds")
    source_end: float = Field(..., ge=0, description="End time in seconds")
    speed: float = Field(1.0, gt=0, description="Playback speed multiplier")
    transition_in: str | None = Field(None, description="Transition into this clip")
    transition_out: str | None = Field(None, description="Transition out of this clip")
    storyboard_shot: str = Field("", description="Storyboard shot reference for traceability")
    intermediate_clip: str = Field("", description="Path to extracted/processed clip")


class TransitionSpec(BaseModel):
    """A transition between two timeline items."""
    after: str = Field(..., description="Clip id this transition follows")
    type: str = Field(..., description="Transition type")
    duration: float = Field(0.0, ge=0, description="Transition duration in seconds")


class AudioPlan(BaseModel):
    """Audio processing plan for the final video."""
    volume: float = Field(1.0, ge=0, description="Master volume multiplier")
    fade_in: float = Field(0.0, ge=0, description="Fade-in duration in seconds")
    fade_out: float = Field(0.0, ge=0, description="Fade-out duration in seconds")
    normalize: bool = Field(False, description="Apply loudnorm normalization")


class EditFormat(BaseModel):
    """Output format specification."""
    width: int = Field(1920, ge=1)
    height: int = Field(1080, ge=1)
    fps: float = Field(30.0, gt=0)


class EditPlan(BaseModel):
    """The complete machine-readable edit plan."""
    version: int = Field(1, ge=1)
    target_duration: float = Field(0.0, ge=0, description="Expected final duration in seconds")
    format: EditFormat = Field(default_factory=EditFormat)
    timeline: list[TimelineItem] = Field(default_factory=list)
    transitions: list[TransitionSpec] = Field(default_factory=list)
    audio: AudioPlan = Field(default_factory=AudioPlan)
    storyboard_version: int = Field(0, description="Storyboard version this edit is based on")
    storyboard_sha: str = Field("", description="Storyboard content hash for traceability")
    output_path: str = Field("", description="Path to the rendered video")
    preset: str = Field("youtube_1080p", description="Render preset used")
    notes: str = Field("", description="Agent's notes about the edit")


# --- Config ------------------------------------------------------------------

@dataclass
class VideoProductionConfig:
    host: str
    api_key: str
    model: str


def load_video_production_config() -> VideoProductionConfig:
    """Read Ollama endpoint config from env vars.

    Uses OLLAMA_WORKFLOW_MODEL (same as Stage 7). Host and API key are
    shared with the vision config.
    """
    return VideoProductionConfig(
        host=os.environ.get("OLLAMA_HOST", "").strip(),
        api_key=os.environ.get("OLLAMA_API_KEY", "").strip(),
        model=os.environ.get("OLLAMA_WORKFLOW_MODEL", "").strip(),
    )


def is_config_valid(config: VideoProductionConfig | None = None
                    ) -> tuple[bool, str]:
    """Return (ok, message). message is "" on success."""
    cfg = config if config is not None else load_video_production_config()
    missing = []
    if not cfg.host:
        missing.append("OLLAMA_HOST")
    if not cfg.model:
        missing.append("OLLAMA_WORKFLOW_MODEL")
    if missing:
        return False, (
            "Ollama configuration is missing or incomplete. "
            f"Set {', '.join(missing)} in your .env file."
        )
    return True, ""


def build_ollama_client(config: VideoProductionConfig):
    """Construct an ollama.Client with host + optional bearer auth."""
    from ollama import Client

    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return Client(host=config.host, headers=headers or None)


# --- Tool result -------------------------------------------------------------

@dataclass
class ToolResult:
    """Result returned by a tool function, sent back to the LLM."""
    success: bool
    data: dict
    log: str = ""
    duration_s: float = 0.0

    def to_tool_message(self) -> dict:
        """Build the 'tool' role message for the Ollama chat."""
        return {
            "role": "tool",
            "content": json.dumps(self.data),
        }


# --- Path safety helpers -----------------------------------------------------

def _sanitize_name(name: str) -> str:
    """Make a filename-safe string (no path separators or special chars)."""
    out = []
    for ch in name:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    return s[:120] if len(s) > 120 else s


def _is_safe_output_path(path: Path, base_dir: Path) -> bool:
    """Ensure path is inside base_dir (no path traversal)."""
    try:
        resolved = path.resolve()
        base_resolved = base_dir.resolve()
        return str(resolved).startswith(str(base_resolved))
    except Exception:
        return False


def _run_ffmpeg(cmd: list[str], timeout: int = 1800) -> tuple[int, str, str]:
    """Run an ffmpeg/ffprobe command, returning (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            check=False, encoding="utf-8", errors="replace",
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


# --- ToolRegistry ------------------------------------------------------------

class ToolRegistry:
    """Holds the 9 domain-specific tools and their execution context.

    Each tool is a method with type hints + docstring. The Ollama SDK derives
    tool schemas from these. Tools validate all inputs before executing FFmpeg.
    """

    def __init__(
        self,
        working_folder: str,
        selected_videos: list,
        metadatas: list,
    ) -> None:
        self._working_folder = Path(working_folder)
        self._video_dir = self._working_folder / VIDEO_DIR
        self._clips_dir = self._video_dir / CLIPS_SUBDIR
        self._output_dir = self._video_dir / OUTPUT_SUBDIR
        self._preview_dir = self._video_dir / PREVIEW_SUBDIR
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._preview_dir.mkdir(parents=True, exist_ok=True)

        self._source_videos: dict[str, Any] = {v.name: v for v in selected_videos}
        self._metadatas = {m.source_filename: m for m in metadatas}
        self._intermediate_clips: dict[str, str] = {}

    def get_tools(self) -> list[Callable]:
        """Return the list of tool functions for the Ollama SDK."""
        return [
            self.probe_video,
            self.inspect_clip,
            self.extract_clip,
            self.create_edit,
            self.create_transition,
            self.mix_audio,
            self.assemble_timeline,
            self.render_video,
            self.validate_video,
        ]

    # --- Tool 1: probe_video -----------------------------------------------

    def probe_video(self, video_path: str) -> dict:
        """Retrieve authoritative technical information about a source video.

        Args:
            video_path: Filename or path of the source video to probe.
        """
        start = time.time()
        fname = Path(video_path).name
        v = self._source_videos.get(fname)
        if v is None:
            return ToolResult(
                False,
                {"error": f"Video '{fname}' is not a selected source video. "
                           f"Available: {list(self._source_videos.keys())}"},
                log=f"probe_video: rejected unknown video '{fname}'",
                duration_s=time.time() - start,
            ).to_tool_message()

        from .video_metadata import extract_metadata
        meta = extract_metadata(v.path)
        if meta is None:
            return ToolResult(
                False,
                {"error": f"ffprobe failed for '{fname}'"},
                log=f"probe_video: ffprobe failed for '{fname}'",
                duration_s=time.time() - start,
            ).to_tool_message()

        data = {
            "filename": meta.source_filename,
            "path": meta.source_path,
            "duration": meta.duration,
            "duration_hms": meta.duration_hms,
            "resolution": f"{meta.width}x{meta.height}" if meta.width else "unknown",
            "frame_rate": meta.frame_rate,
            "video_codec": meta.video_codec,
            "video_profile": meta.video_profile,
            "pixel_format": meta.pixel_format,
            "aspect_ratio": meta.aspect_ratio,
            "video_bitrate": meta.video_bitrate,
            "audio_codec": meta.audio_codec,
            "audio_sample_rate": meta.audio_sample_rate,
            "audio_channels": meta.audio_channels,
            "audio_channel_layout": meta.audio_channel_layout,
            "audio_bitrate": meta.audio_bitrate,
            "container_format": meta.container_format,
        }
        return ToolResult(
            True, data,
            log=f"probe_video: {fname} ({meta.duration_hms}, {meta.width}x{meta.height})",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 2: inspect_clip ------------------------------------------------

    def inspect_clip(self, video_path: str, start_time: float,
                     end_time: float) -> dict:
        """Extract representative frames from a portion of a source video.

        Args:
            video_path: Filename of the source video.
            start_time: Start time in seconds.
            end_time: End time in seconds.
        """
        start = time.time()
        fname = Path(video_path).name
        v = self._source_videos.get(fname)
        if v is None:
            return ToolResult(
                False,
                {"error": f"Video '{fname}' is not a selected source video."},
                log=f"inspect_clip: unknown video '{fname}'",
                duration_s=time.time() - start,
            ).to_tool_message()

        meta = self._metadatas.get(fname)
        duration = meta.duration if meta else 0.0
        if duration > 0:
            if start_time < 0 or end_time > duration or start_time >= end_time:
                return ToolResult(
                    False,
                    {"error": f"Invalid time range {start_time}-{end_time} "
                               f"for video with duration {duration}s"},
                    log="inspect_clip: invalid time range",
                    duration_s=time.time() - start,
                ).to_tool_message()

        clip_dur = end_time - start_time
        interval = max(0.5, clip_dur / 5.0)
        inspect_dir = self._clips_dir / f"inspect_{_sanitize_name(fname)}"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(inspect_dir / "frame_%03d.jpg")

        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-y",
            "-ss", str(start_time),
            "-to", str(end_time),
            "-i", v.path,
            "-vf", f"fps=1/{interval},scale=320:-2",
            "-q:v", "5",
            pattern,
        ]
        rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=120)
        frames = sorted(inspect_dir.glob("frame_*.jpg"))
        frame_data = [
            {"path": str(f), "name": f.name}
            for f in frames
        ]
        if rc != 0 and not frames:
            return ToolResult(
                False,
                {"error": f"FFmpeg failed: {stderr[:500]}"},
                log=f"inspect_clip: ffmpeg failed for {fname}",
                duration_s=time.time() - start,
            ).to_tool_message()

        return ToolResult(
            True,
            {
                "video": fname,
                "start_time": start_time,
                "end_time": end_time,
                "frames_extracted": len(frames),
                "frames": frame_data,
            },
            log=f"inspect_clip: {fname} {start_time}-{end_time} ({len(frames)} frames)",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 3: extract_clip ------------------------------------------------

    def extract_clip(self, video_path: str, start_time: float,
                     end_time: float, output_name: str) -> dict:
        """Create an intermediate clip from source footage.

        Args:
            video_path: Filename of the source video.
            start_time: Start time in seconds.
            end_time: End time in seconds.
            output_name: Name for the output clip (without extension).
        """
        start = time.time()
        fname = Path(video_path).name
        v = self._source_videos.get(fname)
        if v is None:
            return ToolResult(
                False,
                {"error": f"Video '{fname}' is not a selected source video."},
                log=f"extract_clip: unknown video '{fname}'",
                duration_s=time.time() - start,
            ).to_tool_message()

        meta = self._metadatas.get(fname)
        duration = meta.duration if meta else 0.0
        if duration > 0 and (start_time < 0 or end_time > duration):
            return ToolResult(
                False,
                {"error": f"Time range {start_time}-{end_time} exceeds duration {duration}s"},
                log="extract_clip: time range exceeds duration",
                duration_s=time.time() - start,
            ).to_tool_message()
        if start_time >= end_time:
            return ToolResult(
                False,
                {"error": f"start_time ({start_time}) must be < end_time ({end_time})"},
                log="extract_clip: invalid time range",
                duration_s=time.time() - start,
            ).to_tool_message()

        safe_name = _sanitize_name(output_name)
        out_path = self._clips_dir / f"{safe_name}.mp4"
        if not _is_safe_output_path(out_path, self._clips_dir):
            return ToolResult(
                False, {"error": "Output path escaped the clips directory"},
                log="extract_clip: path safety violation",
                duration_s=time.time() - start,
            ).to_tool_message()

        # Always re-encode so rotation metadata (e.g. from phones/GoPros) is
        # physically baked into the pixels. Stream copy (-c copy) preserves
        # the rotation tag as side-data, but downstream tools (concat, xfade)
        # often drop or misapply it, producing upside-down clips.
        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-y",
            "-ss", str(start_time),
            "-to", str(end_time),
            "-i", v.path,
            "-c:v", "libx264", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(out_path),
        ]
        rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=600)
        if rc != 0 or not out_path.exists():
            return ToolResult(
                False,
                {"error": f"FFmpeg failed: {stderr[:500]}"},
                log=f"extract_clip: ffmpeg failed for {safe_name}",
                duration_s=time.time() - start,
            ).to_tool_message()

        self._intermediate_clips[safe_name] = str(out_path)
        return ToolResult(
            True,
            {
                "output_name": safe_name,
                "output_path": str(out_path),
                "source": fname,
                "start_time": start_time,
                "end_time": end_time,
                "duration": end_time - start_time,
            },
            log=f"extract_clip: {fname} {start_time}-{end_time} -> {safe_name}.mp4",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 4: create_edit -------------------------------------------------

    def create_edit(
        self,
        input_clip: str,
        output_name: str,
        trim: dict | None = None,
        speed: float = 1.0,
        crop: str | None = None,
        scale: str | None = None,
        aspect_ratio: str | None = None,
        color_adjustment: dict | None = None,
        audio_adjustment: dict | None = None,
    ) -> dict:
        """Create a processed clip from an intermediate clip with optional transforms.

        Args:
            input_clip: Name of the intermediate clip to process.
            output_name: Name for the output clip (without extension).
            trim: Optional dict with 'start' and 'end' keys (seconds).
            speed: Playback speed multiplier (1.0 = normal).
            crop: Crop specification, e.g. '640:360:100:50'.
            scale: Scale specification, e.g. '1920x1080' or '1280:-2'.
            aspect_ratio: Target aspect ratio, e.g. '16:9'.
            color_adjustment: Optional dict with brightness, contrast, saturation, gamma.
            audio_adjustment: Optional dict with volume, fade_in, fade_out.
        """
        start = time.time()
        in_path = self._resolve_clip(input_clip)
        if in_path is None:
            return ToolResult(
                False,
                {"error": f"Input clip '{input_clip}' not found"},
                log="create_edit: input clip not found",
                duration_s=time.time() - start,
            ).to_tool_message()

        safe_name = _sanitize_name(output_name)
        out_path = self._clips_dir / f"{safe_name}.mp4"
        if not _is_safe_output_path(out_path, self._clips_dir):
            return ToolResult(
                False, {"error": "Output path escaped the clips directory"},
                duration_s=time.time() - start,
            ).to_tool_message()

        vf_parts: list[str] = []
        af_parts: list[str] = []
        input_args: list[str] = []

        if trim:
            t_start = trim.get("start", 0.0)
            t_end = trim.get("end", 0.0)
            input_args = ["-ss", str(t_start), "-to", str(t_end)]

        if speed and speed != 1.0:
            vf_parts.append(f"setpts={1.0/speed:.4f}*PTS")
            atempo = speed
            while atempo > 2.0:
                af_parts.append("atempo=2.0")
                atempo /= 2.0
            while atempo < 0.5:
                af_parts.append("atempo=0.5")
                atempo *= 2.0
            af_parts.append(f"atempo={atempo:.4f}")

        if crop:
            vf_parts.append(f"crop={crop}")

        if scale:
            vf_parts.append(f"scale={scale.replace('x', ':')}")

        if aspect_ratio:
            vf_parts.append(f"setdar={aspect_ratio}")

        if color_adjustment:
            eq_parts = []
            if "brightness" in color_adjustment:
                eq_parts.append(f"brightness={color_adjustment['brightness']}")
            if "contrast" in color_adjustment:
                eq_parts.append(f"contrast={color_adjustment['contrast']}")
            if "saturation" in color_adjustment:
                eq_parts.append(f"saturation={color_adjustment['saturation']}")
            if "gamma" in color_adjustment:
                eq_parts.append(f"gamma={color_adjustment['gamma']}")
            if eq_parts:
                vf_parts.append(f"eq={':'.join(eq_parts)}")

        if audio_adjustment:
            vol = audio_adjustment.get("volume", 1.0)
            if vol != 1.0:
                af_parts.append(f"volume={vol}")
            fade_in = audio_adjustment.get("fade_in", 0.0)
            if fade_in > 0:
                af_parts.append(f"afade=t=in:d={fade_in}")
            fade_out = audio_adjustment.get("fade_out", 0.0)
            if fade_out > 0:
                af_parts.append(f"afade=t=out:d={fade_out}")

        cmd = [_ffmpeg_bin(), "-hide_banner", "-y"] + input_args + ["-i", str(in_path)]
        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]
        if af_parts:
            cmd += ["-af", ",".join(af_parts)]
        cmd += ["-c:v", "libx264", "-crf", "18", "-c:a", "aac", str(out_path)]

        rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=600)
        if rc != 0 or not out_path.exists():
            return ToolResult(
                False,
                {"error": f"FFmpeg failed: {stderr[:500]}",
                 "command": " ".join(cmd)},
                log=f"create_edit: ffmpeg failed for {safe_name}",
                duration_s=time.time() - start,
            ).to_tool_message()

        self._intermediate_clips[safe_name] = str(out_path)
        applied = {
            "speed": speed, "crop": crop, "scale": scale,
            "color": bool(color_adjustment), "audio": bool(audio_adjustment),
        }
        return ToolResult(
            True,
            {"output_name": safe_name, "output_path": str(out_path),
             "applied": applied},
            log=f"create_edit: {input_clip} -> {safe_name}.mp4",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 5: create_transition ------------------------------------------

    def create_transition(self, clip_a: str, clip_b: str, transition: str,
                          duration: float, output_name: str) -> dict:
        """Create a transition between two clips using xfade.

        Args:
            clip_a: Name of the first clip.
            clip_b: Name of the second clip.
            transition: Transition type (cut, dissolve, fadeblack, etc.).
            duration: Transition duration in seconds.
            output_name: Name for the output clip (without extension).
        """
        start = time.time()

        try:
            duration = float(duration)
        except (TypeError, ValueError):
            return ToolResult(
                False,
                {"error": f"Invalid transition duration: {duration!r}. Expected a number."},
                log="create_transition: invalid duration",
                duration_s=time.time() - start,
            ).to_tool_message()

        if duration <= 0:
            return ToolResult(
                False,
                {"error": "Transition duration must be greater than 0."},
                log="create_transition: invalid duration",
                duration_s=time.time() - start,
            ).to_tool_message()

        if transition not in SUPPORTED_TRANSITIONS:
            return ToolResult(
                False,
                {"error": f"Unsupported transition '{transition}'. "
                           f"Supported: {sorted(SUPPORTED_TRANSITIONS)}"},
                log=f"create_transition: unsupported type '{transition}'",
                duration_s=time.time() - start,
            ).to_tool_message()

        path_a = self._resolve_clip(clip_a)
        path_b = self._resolve_clip(clip_b)
        if path_a is None or path_b is None:
            missing = clip_a if path_a is None else clip_b
            return ToolResult(
                False,
                {"error": f"Clip '{missing}' not found"},
                log="create_transition: clip not found",
                duration_s=time.time() - start,
            ).to_tool_message()

        safe_name = _sanitize_name(output_name)
        out_path = self._clips_dir / f"{safe_name}.mp4"

        if transition == "cut":
            cmd = [
                _ffmpeg_bin(), "-hide_banner", "-y",
                "-i", str(path_a), "-i", str(path_b),
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0[v];"
                "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-crf", "18", "-c:a", "aac",
                str(out_path),
            ]
        else:
            dur_a = self._probe_duration(path_a)
            offset = max(0.0, dur_a - duration)
            cmd = [
                _ffmpeg_bin(), "-hide_banner", "-y",
                "-i", str(path_a), "-i", str(path_b),
                "-filter_complex",
                f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
                f"[0:a][1:a]acrossfade=d={duration}[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-crf", "18", "-c:a", "aac",
                str(out_path),
            ]

        rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=600)
        if rc != 0 or not out_path.exists():
            return ToolResult(
                False,
                {"error": f"FFmpeg failed: {stderr[:500]}",
                 "command": " ".join(cmd)},
                log=f"create_transition: ffmpeg failed for {safe_name}",
                duration_s=time.time() - start,
            ).to_tool_message()

        self._intermediate_clips[safe_name] = str(out_path)
        return ToolResult(
            True,
            {"output_name": safe_name, "output_path": str(out_path),
             "transition": transition, "duration": duration},
            log=f"create_transition: {clip_a} + {clip_b} -> {safe_name}.mp4 ({transition})",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 6: mix_audio ---------------------------------------------------

    def mix_audio(self, video_clip: str, audio_sources: list,
                   volumes: list, fades: dict | None = None,
                   normalization: bool = False) -> dict:
        """Mix audio tracks into a video clip.

        Args:
            video_clip: Name of the video clip to mix audio into.
            audio_sources: List of audio source clip names.
            volumes: List of volume multipliers (0.0-2.0), one per audio source.
            fades: Optional dict with 'fade_in' and 'fade_out' durations.
            normalization: If true, apply loudnorm normalization.
        """
        start = time.time()
        video_path = self._resolve_clip(video_clip)
        if video_path is None:
            return ToolResult(
                False,
                {"error": f"Video clip '{video_clip}' not found"},
                log="mix_audio: video clip not found",
                duration_s=time.time() - start,
            ).to_tool_message()

        if len(audio_sources) != len(volumes):
            return ToolResult(
                False,
                {"error": "audio_sources and volumes must have the same length"},
                log="mix_audio: length mismatch",
                duration_s=time.time() - start,
            ).to_tool_message()

        audio_paths = [self._resolve_clip(a) for a in audio_sources]
        if any(p is None for p in audio_paths):
            return ToolResult(
                False,
                {"error": "One or more audio sources not found"},
                log="mix_audio: audio source not found",
                duration_s=time.time() - start,
            ).to_tool_message()

        safe_name = _sanitize_name(video_clip) + "_mixed"
        out_path = self._clips_dir / f"{safe_name}.mp4"

        # Build the filter_complex graph. Input 0 is the video clip, inputs
        # 1..N are audio sources. We must label each stream explicitly.
        n_sources = len(audio_sources) + 1  # video audio + audio sources
        audio_labels = [f"[{i}:a]" for i in range(n_sources)]
        weights = " ".join(str(v) for v in [1.0] + volumes)

        filter_parts: list[str] = []
        # Mix all audio streams together
        filter_parts.append(
            f"{''.join(audio_labels)}amix=inputs={n_sources}:duration=first:weights={weights}[mixed]"
        )

        # Apply fades on the mixed result
        fade_chain = "[mixed]"
        if fades:
            fi = fades.get("fade_in", 0.0)
            fo = fades.get("fade_out", 0.0)
            if fi > 0:
                filter_parts.append(f"[mixed]afade=t=in:d={fi}[faded]")
                fade_chain = "[faded]"
            if fo > 0:
                # fade_out needs a start time — use a large value that
                # afade will clamp; the tool doesn't know total duration here
                # so we apply it as a chain on the faded/mixed output
                in_label = fade_chain
                fade_chain = "[norm]"
                filter_parts.append(f"{in_label}afade=t=out:d={fo}{fade_chain}")

        if normalization:
            in_label = fade_chain
            fade_chain = "[final_a]"
            filter_parts.append(f"{in_label}loudnorm=I=-16:TP=-1.5:LRA=11{fade_chain}")

        # Final audio label (whatever the last chain produced, or [mixed])
        final_a_label = fade_chain if fade_chain != "[mixed]" else "[mixed]"

        cmd = [_ffmpeg_bin(), "-hide_banner", "-y"]
        cmd += ["-i", str(video_path)]
        for p in audio_paths:
            cmd += ["-i", str(p)]
        cmd += ["-filter_complex", ";".join(filter_parts)]
        cmd += ["-map", "0:v", "-map", final_a_label]
        cmd += ["-c:v", "copy", "-c:a", "aac", str(out_path)]

        rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=600)
        if rc != 0 or not out_path.exists():
            return ToolResult(
                False,
                {"error": f"FFmpeg failed: {stderr[:500]}"},
                log="mix_audio: ffmpeg failed",
                duration_s=time.time() - start,
            ).to_tool_message()

        self._intermediate_clips[safe_name] = str(out_path)
        return ToolResult(
            True,
            {"output_name": safe_name, "output_path": str(out_path),
             "sources_mixed": len(audio_sources)},
            log=f"mix_audio: {video_clip} + {len(audio_sources)} sources -> {safe_name}",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 7: assemble_timeline -------------------------------------------

    def assemble_timeline(self, clips: list, transitions: list | None = None,
                          audio_plan: dict | None = None,
                          output_name: str = "timeline") -> dict:
        """Assemble multiple clips into a single timeline video.

        Args:
            clips: List of clip names in order.
            transitions: List of transition specs with 'after', 'type', 'duration'.
            audio_plan: Optional dict with volume, fade_in, fade_out, normalize.
            output_name: Name for the output (without extension).
        """
        start = time.time()
        if not clips:
            return ToolResult(
                False, {"error": "No clips provided"},
                log="assemble_timeline: no clips",
                duration_s=time.time() - start,
            ).to_tool_message()

        clip_paths = [self._resolve_clip(c) for c in clips]
        if any(p is None for p in clip_paths):
            missing = [c for c, p in zip(clips, clip_paths) if p is None]
            return ToolResult(
                False,
                {"error": f"Clips not found: {missing}"},
                log=f"assemble_timeline: missing clips {missing}",
                duration_s=time.time() - start,
            ).to_tool_message()

        safe_name = _sanitize_name(output_name)
        out_path = self._clips_dir / f"{safe_name}.mp4"

        has_transitions = bool(transitions)
        if has_transitions:
            # Build a filter_complex graph with labeled streams.
            # For N clips, we chain N-1 xfade/concat operations.
            # Input i → [i:v] and [i:a].
            filter_parts: list[str] = []
            input_args: list[str] = []
            for i, p in enumerate(clip_paths):
                input_args += ["-i", str(p)]

            # Normalize all inputs to the same resolution/fps so concat and
            # xfade work correctly. The first clip's resolution is the target.
            # Each input gets scaled and fps-converted.
            norm_parts: list[str] = []
            for i in range(len(clip_paths)):
                norm_parts.append(
                    f"[{i}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,fps=30[nv{i}]"
                )
                norm_parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[na{i}]")
            filter_parts.extend(norm_parts)

            prev_v = "[nv0]"
            prev_a = "[na0]"
            for i in range(1, len(clip_paths)):
                trans = transitions[i-1] if i-1 < len(transitions) else {"type": "cut", "duration": 0.0}
                t_type = trans.get("type", "cut")
                t_dur = trans.get("duration", 0.0)
                is_last = i == len(clip_paths) - 1
                out_v = "[vout]" if is_last else f"[v{i}]"
                out_a = "[aout]" if is_last else f"[a{i}]"

                if t_type == "cut" or t_dur <= 0:
                    # concat two streams
                    filter_parts.append(
                        f"{prev_v}[nv{i}]concat=n=2:v=1:a=0{out_v};"
                        f"{prev_a}[na{i}]concat=n=2:v=0:a=1{out_a}"
                    )
                else:
                    # xfade for video, acrossfade for audio
                    dur_prev = self._probe_duration(clip_paths[i-1])
                    offset = max(0.0, dur_prev - t_dur)
                    filter_parts.append(
                        f"{prev_v}[nv{i}]xfade=transition={t_type}:duration={t_dur}:offset={offset}{out_v};"
                        f"{prev_a}[na{i}]acrossfade=d={t_dur}{out_a}"
                    )
                prev_v = out_v
                prev_a = out_a

            cmd = [_ffmpeg_bin(), "-hide_banner", "-y"] + input_args
            cmd += ["-filter_complex", ";".join(filter_parts)]
            cmd += ["-map", "[vout]", "-map", "[aout]"]
            cmd += ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(out_path)]
        else:
            # No transitions — use concat demuxer but RE-ENCODE so rotation
            # metadata is physically baked in (stream copy drops rotation
            # side-data from some formats, causing upside-down clips).
            concat_list = self._clips_dir / f"{safe_name}_concat.txt"
            with open(concat_list, "w", encoding="utf-8") as f:
                for p in clip_paths:
                    f.write(f"file '{p}'\n")
            cmd = [
                _ffmpeg_bin(), "-hide_banner", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                str(out_path),
            ]

        rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=600)
        if rc != 0 or not out_path.exists():
            return ToolResult(
                False,
                {"error": f"FFmpeg failed: {stderr[:500]}",
                 "command": " ".join(cmd[-200:])},
                log="assemble_timeline: ffmpeg failed",
                duration_s=time.time() - start,
            ).to_tool_message()

        self._intermediate_clips[safe_name] = str(out_path)
        return ToolResult(
            True,
            {"output_name": safe_name, "output_path": str(out_path),
             "clips_assembled": len(clips),
             "transitions": len(transitions) if transitions else 0},
            log=f"assemble_timeline: {len(clips)} clips -> {safe_name}.mp4",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 8: render_video ------------------------------------------------

    def render_video(self, timeline: str, output_path: str,
                     resolution: str = "1920x1080",
                     frame_rate: float = 30.0,
                     video_codec: str = "h264",
                     audio_codec: str = "aac",
                     preset: str = "youtube_1080p") -> dict:
        """Render the final video from an assembled timeline.

        Args:
            timeline: Name of the assembled timeline clip to render.
            output_path: Filename for the final output (e.g. 'final.mp4').
            resolution: Target resolution as WxH.
            frame_rate: Target frame rate.
            video_codec: Video codec (h264 or h265).
            audio_codec: Audio codec (aac or opus).
            preset: Render preset (preview, youtube_1080p, youtube_4k, high_quality).
        """
        start = time.time()
        if preset not in SUPPORTED_PRESETS:
            return ToolResult(
                False,
                {"error": f"Unsupported preset '{preset}'. Supported: {sorted(SUPPORTED_PRESETS)}"},
                log=f"render_video: unsupported preset '{preset}'",
                duration_s=time.time() - start,
            ).to_tool_message()

        in_path = self._resolve_clip(timeline)
        if in_path is None:
            return ToolResult(
                False,
                {"error": f"Timeline clip '{timeline}' not found"},
                log="render_video: timeline not found",
                duration_s=time.time() - start,
            ).to_tool_message()

        safe_name = _sanitize_name(Path(output_path).name)
        out_path = self._output_dir / safe_name
        if not _is_safe_output_path(out_path, self._output_dir):
            return ToolResult(
                False, {"error": "Output path escaped the output directory"},
                duration_s=time.time() - start,
            ).to_tool_message()

        profile = PRESET_PROFILES[preset]
        vf_parts = [f"scale={resolution.replace('x', ':')}"]
        vf_parts.append(f"fps={frame_rate}")

        # Build the codec list. Try NVENC first (if available), then fall
        # back to software libx264/libx265 if the NVENC encode fails.
        nvenc_available = is_nvenc_available()
        sw_vcodec = "libx264"
        hw_vcodec = "h264_nvenc"
        if video_codec == "h265":
            sw_vcodec = "libx265"
            hw_vcodec = "hevc_nvenc"

        # Determine which encoder to try first
        if nvenc_available:
            vcodec = hw_vcodec
        else:
            vcodec = sw_vcodec

        def _build_cmd(enc: str) -> list[str]:
            c = [
                _ffmpeg_bin(), "-hide_banner", "-y",
                "-i", str(in_path),
                "-vf", ",".join(vf_parts),
                "-c:v", enc,
            ]
            if enc.startswith(("h264_nvenc", "hevc_nvenc")):
                c += ["-preset", "p4", "-cq", profile["crf"]]
            elif enc == "libx264":
                c += ["-crf", profile["crf"], "-preset", profile["preset"]]
            elif enc == "libx265":
                c += ["-crf", str(int(profile["crf"]) + 5), "-preset", profile["preset"]]
            c += profile.get("extra", [])
            if "pix_fmt" in profile:
                c += ["-pix_fmt", profile["pix_fmt"]]
            c += ["-c:a", audio_codec, "-b:a", profile["abitrate"]]
            if profile.get("faststart"):
                c += ["-movflags", "+faststart"]
            c += [str(out_path)]
            return c

        cmd = _build_cmd(vcodec)
        rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=3600)

        # If NVENC failed, fall back to software encoder
        if rc != 0 and vcodec != sw_vcodec:
            vcodec = sw_vcodec
            cmd = _build_cmd(vcodec)
            rc, _stdout, stderr = _run_ffmpeg(cmd, timeout=3600)

        if rc != 0 or not out_path.exists():
            return ToolResult(
                False,
                {"error": f"FFmpeg failed: {stderr[:500]}",
                 "command": " ".join(cmd[-200:])},
                log="render_video: ffmpeg failed",
                duration_s=time.time() - start,
            ).to_tool_message()

        return ToolResult(
            True,
            {"output_path": str(out_path), "preset": preset,
             "resolution": resolution, "frame_rate": frame_rate,
             "codec": vcodec},
            log=f"render_video: {timeline} -> {safe_name} ({preset})",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 9: validate_video ----------------------------------------------

    def validate_video(self, video_path: str,
                        expected_duration: float | None = None,
                        expected_resolution: str | None = None) -> dict:
        """Validate a rendered video file using ffprobe.

        Args:
            video_path: Path to the video to validate.
            expected_duration: Optional expected duration in seconds.
            expected_resolution: Optional expected resolution as WxH.
        """
        start = time.time()
        p = Path(video_path)
        if not p.is_absolute():
            p = self._output_dir / video_path
        if not p.exists():
            return ToolResult(
                False,
                {"error": f"File does not exist: {p}"},
                log="validate_video: file not found",
                duration_s=time.time() - start,
            ).to_tool_message()

        from .ffmpeg.probe import run_ffprobe
        result = run_ffprobe(str(p))
        if result is None:
            return ToolResult(
                False,
                {"error": "ffprobe failed — file may be corrupt or unplayable"},
                log="validate_video: ffprobe failed",
                duration_s=time.time() - start,
            ).to_tool_message()

        issues: list[str] = []
        if result.duration <= 0:
            issues.append("Zero or negative duration")
        if expected_duration and abs(result.duration - expected_duration) > 1.0:
            issues.append(
                f"Duration mismatch: expected {expected_duration}s, got {result.duration}s"
            )
        if expected_resolution:
            exp_w, exp_h = expected_resolution.split("x")
            if result.width != int(exp_w) or result.height != int(exp_h):
                issues.append(
                    f"Resolution mismatch: expected {expected_resolution}, "
                    f"got {result.width}x{result.height}"
                )

        passed = len(issues) == 0
        data = {
            "passed": passed,
            "duration": result.duration,
            "resolution": f"{result.width}x{result.height}",
            "codec": result.codec,
            "fps": result.fps,
            "issues": issues,
            "path": str(p),
        }
        return ToolResult(
            passed, data,
            log=f"validate_video: {'PASS' if passed else 'FAIL: ' + '; '.join(issues)}",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Internal helpers ----------------------------------------------------

    def _resolve_clip(self, name: str) -> Path | None:
        """Resolve a clip name to its file path.

        Checks intermediate clips first, then the clips directory.
        """
        if name in self._intermediate_clips:
            p = Path(self._intermediate_clips[name])
            if p.exists():
                return p

        for ext in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
            p = self._clips_dir / f"{name}{ext}"
            if p.exists():
                return p
        return None

    def _probe_duration(self, path: Path) -> float:
        """Get the duration of a clip via ffprobe."""
        from .ffmpeg.probe import run_ffprobe
        result = run_ffprobe(str(path))
        return result.duration if result else 0.0


# --- Agent loop ---------------------------------------------------------------

def run_editing_agent(
    client,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list,
    progress_cb: Callable[[str], None] | None = None,
    log_cb: Callable[[str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[str, list[dict]]:
    """Run the multi-turn tool-calling agent loop.

    Returns (final_text, tool_log) where tool_log is a list of dicts with
    tool name, args, result, success, and duration.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tool_log: list[dict] = []
    final_text = ""

    for round_trip in range(MAX_AGENT_ROUND_TRIPS):
        if is_cancelled and is_cancelled():
            return "Cancelled", tool_log

        if progress_cb:
            progress_cb(f"Agent round {round_trip + 1}/{MAX_AGENT_ROUND_TRIPS}...")

        response = client.chat(model=model, messages=messages, tools=tools)
        assistant_msg = response.message
        messages.append(assistant_msg)

        tool_calls = getattr(assistant_msg, "tool_calls", None)
        if not tool_calls:
            final_text = getattr(assistant_msg, "content", "") or ""
            break

        for call in tool_calls:
            tool_name = call.function.name
            args = call.function.arguments
            args_str = json.dumps(args) if isinstance(args, dict) else str(args)

            if log_cb:
                log_cb(f"[tool] {tool_name}({args_str})")

            round_start = time.time()
            result_data = _execute_tool_call(tools, tool_name, args)
            round_dur = time.time() - round_start

            tool_log.append({
                "tool": tool_name,
                "args": args,
                "result": result_data.get("data", result_data),
                "success": not bool(result_data.get("error")),
                "duration_s": round_dur,
                "timestamp": _now_iso(),
            })

            if log_cb:
                status = "OK" if not result_data.get("error") else f"FAIL: {result_data.get('error', '')[:100]}"
                log_cb(f"[tool] {tool_name} -> {status}")

            messages.append({
                "role": "tool",
                "content": json.dumps(result_data),
            })
    else:
        if log_cb:
            log_cb(f"Agent loop exhausted at {MAX_AGENT_ROUND_TRIPS} round trips")
        final_text = "Agent loop did not complete within the maximum number of round trips."

    return final_text, tool_log


def _execute_tool_call(tools: list, tool_name: str, args: Any) -> dict:
    """Execute a tool call by dispatching to the matching function."""
    tool_func = None
    for t in tools:
        if getattr(t, "__name__", "") == tool_name:
            tool_func = t
            break
    if tool_func is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        if isinstance(args, dict):
            result = tool_func(**args)
        else:
            result = tool_func(args)
        if isinstance(result, dict):
            return result
        return {"result": str(result)}
    except Exception as e:
        return {"error": f"Tool execution error: {e}"}


# --- Prompt building ---------------------------------------------------------

def build_generation_prompt(storyboard_md: str, context_md: str) -> str:
    """Build the user-message content for an initial edit generation."""
    return (
        f"## Storyboard\n\n{storyboard_md.strip()}\n\n"
        f"## Available Context\n\n{context_md.strip()}\n\n"
        f"## Task\n\n"
        f"Produce a final edited video based on the storyboard. Follow the "
        f"editing workflow: probe all source videos, extract clips, apply "
        f"edits and transitions, assemble the timeline, render, and validate. "
        f"Use the tools provided — do not write FFmpeg commands. "
        f"Reference source videos by filename. Do not invent footage. "
        f"Report any gaps or issues you encounter."
    )


def build_refinement_prompt(feedback: str, edit_plan_json: str,
                             storyboard_md: str, context_md: str) -> str:
    """Build the user-message content for a refinement based on user feedback."""
    return (
        f"{REFINEMENT_INSTRUCTIONS.strip()}\n\n"
        f"## User Feedback\n\n{feedback.strip()}\n\n"
        f"## Current Edit Plan\n\n```json\n{edit_plan_json}\n```\n\n"
        f"## Storyboard\n\n{storyboard_md.strip()}\n\n"
        f"## Available Context\n\n{context_md.strip()}\n\n"
        f"## Task\n\n"
        f"Apply the user's feedback to refine the existing video edit. "
        f"Preserve good decisions from the current edit. Modify only what "
        f"the user has requested. Use the tools to make changes and re-render."
    )


# --- Persistence -------------------------------------------------------------

def _video_dir(working_folder: str) -> Path:
    return Path(working_folder) / VIDEO_DIR


def save_edit_plan(working_folder: str, plan: EditPlan) -> Path:
    """Persist the edit plan to video/edit_plan.json."""
    d = _video_dir(working_folder)
    d.mkdir(parents=True, exist_ok=True)
    p = d / EDIT_PLAN_FILENAME
    p.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return p


def load_edit_plan(working_folder: str) -> EditPlan | None:
    """Load the edit plan. Returns None if not found or invalid."""
    p = _video_dir(working_folder) / EDIT_PLAN_FILENAME
    if not p.exists():
        return None
    try:
        return EditPlan.model_validate_json(p.read_text(encoding="utf-8"))
    except (ValidationError, OSError):
        return None


def save_tool_log(working_folder: str, log: list[dict]) -> Path:
    """Persist the tool execution log to video/tool_log.json."""
    d = _video_dir(working_folder)
    d.mkdir(parents=True, exist_ok=True)
    p = d / TOOL_LOG_FILENAME
    p.write_text(json.dumps(log, indent=2), encoding="utf-8")
    return p


def load_tool_log(working_folder: str) -> list[dict]:
    """Load the tool log. Returns [] if not found."""
    p = _video_dir(working_folder) / TOOL_LOG_FILENAME
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def clear_production(working_folder: str) -> None:
    """Delete all video production artefacts.

    Removes the entire <working_folder>/video/ directory.
    Does not raise if the directory does not exist.
    """
    d = _video_dir(working_folder)
    if not d.exists():
        return
    shutil.rmtree(d, ignore_errors=True)


# --- VideoProductionSettings --------------------------------------------------

from dataclasses import dataclass as dc_dataclass, asdict


@dc_dataclass
class VideoProductionSettings:
    """Persisted per-project video production UI settings."""
    last_feedback: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VideoProductionSettings":
        fields = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


# --- Helpers -----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build_edit_plan_from_tool_log(tool_log: list[dict], storyboard_version: int = 0,
                                  storyboard_sha: str = "") -> EditPlan:
    """Attempt to build an EditPlan from the tool call log.

    Extracts timeline items from extract_clip calls and transitions from
    create_transition calls.
    """
    timeline: list[TimelineItem] = []
    transitions: list[TransitionSpec] = []
    shot_num = 0

    for entry in tool_log:
        tool = entry.get("tool", "")
        args = entry.get("args", {})
        result = entry.get("result", {})
        if tool == "extract_clip" and not result.get("error"):
            shot_num += 1
            timeline.append(TimelineItem(
                id=f"shot_{shot_num:02d}",
                source=args.get("video_path", ""),
                source_start=args.get("start_time", 0.0),
                source_end=args.get("end_time", 0.0),
                speed=1.0,
                intermediate_clip=result.get("output_name", ""),
            ))
        elif tool == "create_transition" and not result.get("error"):
            transitions.append(TransitionSpec(
                after=f"shot_{shot_num:02d}" if shot_num > 0 else "",
                type=args.get("transition", "cut"),
                duration=args.get("duration", 0.0),
            ))
        elif tool == "render_video" and not result.get("error"):
            pass

    return EditPlan(
        version=1,
        target_duration=0.0,
        timeline=timeline,
        transitions=transitions,
        storyboard_version=storyboard_version,
        storyboard_sha=storyboard_sha,
        output_path="",
    )
