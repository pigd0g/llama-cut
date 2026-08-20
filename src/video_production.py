"""Stage 8 — Final Video Production engine (chat-driven).

Pure-logic module containing:
  * Ollama configuration (reuses OLLAMA_WORKFLOW_MODEL)
  * Pydantic edit-plan models: beats (timeline) + commands (ffmpeg ops)
  * ToolRegistry with 4 planning-only tools (probe, inspect, commit, update)
  * Chat persistence (chat.json) + edit-plan persistence (edit_plan.json)
  * Beat thumbnail extraction helper

The agent converses with the user via the chat UI to BUILD an edit plan
consisting of typed ffmpeg commands. It never runs ffmpeg. The
EditPlanExecutor (src/edit_plan_executor.py) runs the queued commands
sequentially with weighted progress and safe abort.

Stage 8 V2 philosophy
---------------------

Goal: accurate → inspectable → recoverable, with the human in the loop.

The agent is a careful *planner*, not an autonomous executor. It helps the
user translate the storyboard into a structured Edit Plan (beats + commands),
refines it through conversation, and reports execution failures back to the
user so the agent can propose fixes. Execution is a separate, deterministic
phase driven by EditPlanExecutor.

Priority order (enforced in the system prompt):
  1. Correctly interpret the storyboard
  2. Select the correct source footage
  3. Respect exact timestamps
  4. Produce the intended sequence
  5. Apply transitions/effects correctly
  6. Construct correct ffmpeg commands
  7. Only then optimise

Deferred (do NOT implement in V2): proxy/draft-resolution workflows,
aggressive caching, single-pass filter graphs, GPU tuning beyond automatic
NVENC fallback, parallel rendering, intermediate-file elimination.
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
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from . import paths


# --- Constants ---------------------------------------------------------------

MAX_AGENT_ROUND_TRIPS = 80

VIDEO_DIR = paths.VIDEO_DIR_NAME
CLIPS_SUBDIR = "clips"
OUTPUT_SUBDIR = "output"
PREVIEW_SUBDIR = "preview"
THUMBS_SUBDIR = "thumbs"
EDIT_PLAN_FILENAME = "edit_plan.json"
TOOL_LOG_FILENAME = "tool_log.json"
CHAT_FILENAME = "chat.json"

SUPPORTED_TRANSITIONS = frozenset({
    "cut", "dissolve", "fadeblack", "fadewhite", "fadegrays",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circleopen", "circleclose", "zoomin",
})

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

# Execution stage weights (sum to 1.0) for progress calculation.
# Weighted by video/clip duration so a 30-min clip contributes more than a
# 1-min clip. See EditPlanExecutor for the work-unit mapping per stage.
STAGE_WEIGHTS: dict[str, float] = {
    "extract": 0.20,
    "transitions": 0.10,
    "assemble": 0.15,
    "audio": 0.05,
    "render": 0.40,
    "validate": 0.10,
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
You are an expert video editing assistant. You help the user build an edit \
plan from their approved storyboard. You do NOT run ffmpeg — you construct a \
structured Edit Plan (beats + commands) that the user reviews and then \
executes.

You are a CAREFUL planner. Prioritise correctness and inspectability. \
Intermediate clips are kept on disk as checkpoints \
(shot01_hook.mp4, shot02a_ducks.mp4, ...) so a failure can be localised to \
an exact step.

## Your Role

You converse with the user through a chat interface. The storyboard and \
assembled context are provided to you up front — you do not need to ask for \
them. Your job is to:

  1. Interpret the storyboard and translate it into a sequence of BEATS \
     (the narrative timeline the user will see visually).
  2. For each beat, decide the exact source footage and time range.
  3. Construct the FFmpeg COMMANDS that will realise those beats, in the \
     correct execution order, using the FFmpeg reference provided.
  4. Present the plan via commit_edit_plan() and refine it based on the \
     user's feedback (via update_edit_plan()).

When the chat is empty, be proactive: greet the user, summarise what you \
see in the storyboard, and offer a few concrete starting points \
(conversation starters) so they know how to direct you. Be approachable \
and low-pressure.

## Priority Order

  1. Correctly interpret the storyboard
  2. Select the correct source footage
  3. Respect exact timestamps
  4. Produce the intended sequence
  5. Apply transitions/effects correctly
  6. Construct correct ffmpeg commands
  7. Only then optimise

## Tools

You have FOUR tools:

  - probe_video(filename): Get authoritative technical metadata (duration, \
    resolution, fps, codec, audio) for a source video. ALWAYS call this \
    before referencing a source's timestamps.
  - inspect_clip(filename, start, end): Extract representative frames from \
    a source range to verify boundaries when the storyboard's exact \
    timestamps are uncertain.
  - commit_edit_plan(plan): Commit the full Edit Plan (beats + commands). \
    This is how you present a plan to the user.
  - update_edit_plan(plan): Amend the plan based on user feedback or after \
    an execution failure. Replaces the current plan entirely.

You do NOT have tools for extracting clips, creating transitions, \
assembling, rendering, or validating — those are executed deterministically \
from the commands you put in the plan.

## The Edit Plan

The plan has two linked parts:

### timeline (BEATS — for visual display)

An ordered list of shots. Each beat:
  - id: unique shot id, e.g. "shot01_hook"
  - source: exact source filename (must be a probed video)
  - source_start, source_end: time range in seconds within the source
  - speed: playback speed (default 1.0)
  - storyboard_scene, storyboard_shot: traceability to the storyboard
  - purpose: why this shot is used (shown as the annotation)
  - description: what the shot shows (shown as the annotation)
  - transition_out: transition to the NEXT beat (cut, dissolve, ...) or null

### commands (FFMPEG OPS — for execution)

An ordered list of typed ffmpeg operations. Each command has:
  - id: unique command id, e.g. "cmd01"
  - type: one of "extract_clip", "create_transition", "create_edit", \
    "assemble_timeline", "mix_audio", "render_video", "validate"
  - beat_id: the timeline beat this command produces (for extract_clip, \
    create_edit) or links to. Use null for assembly/render/validate.
  - args: the typed arguments for that command type (see below).

Command types and their args:

  extract_clip:
    {source, start_time, end_time, output_name}
    Always re-encode (libx264 + aac) to bake rotation metadata.

  create_edit:
    {input_clip, output_name, trim?, speed?, crop?, scale?, \
     aspect_ratio?, color_adjustment?, audio_adjustment?}
    Applies transforms to an intermediate clip.

  create_transition:
    {clip_a, clip_b, transition, duration, output_name}
    transition must be one of the supported xfade types.

  assemble_timeline:
    {clips: [shot clip names], transitions: [{after, type, duration}], \
     output_name}
    Concatenates shot clips with transitions. Do NOT include transition \
    clips in the clips list — describe transitions in the transitions array.

  mix_audio:
    {video_clip, audio_sources: [names], volumes: [floats], \
     fades?: {fade_in, fade_out}, normalization?: bool}

  render_video:
    {timeline, output_name, resolution, frame_rate, video_codec, \
     audio_codec, preset}
    preset must be one of: preview, youtube_1080p, youtube_4k, high_quality.

  validate:
    {target, kind: "clip"|"video", expected_duration?, \
     expected_resolution?, expected_fps?, require_audio?}

### Output format (resolution / frame rate / preset)

The Edit Plan has a `format` object (width, height, fps) and a `preset` \
field. These MUST reflect the user's explicit requirements:
  - If the brief says "4k"/"4K"/"3840x2160", set format to \
{width: 3840, height: 2160} and preset to "youtube_4k".
  - If the brief says "1080p"/"1920x1080", set format to \
{width: 1920, height: 1080} and preset to "youtube_1080p".
  - If the brief specifies a frame rate (e.g. "60fps", "24fps"), set \
format.fps accordingly; otherwise use the source frame rate or 30.0.
  - If the brief does not specify, use the default preset (youtube_1080p, \
30fps). Never silently downgrade a 4K request to 1080p.

## Accuracy Rules

### Timestamps
- NEVER assume a timestamp. Before referencing a source, call probe_video() \
  and read its real duration.
- Before including an extract_clip command, validate: start >= 0, \
  end <= duration, start < end.
- If a storyboard timestamp falls outside the source duration, report the \
  problem rather than truncating the range.

### Traceability
- Every extract_clip command's output_name MUST match a timeline beat's id \
  so each beat is traceable to its generated clip.
- Name clips after shots: shot01_hook, shot02a_ducks, shot02b_boat, ...

### Timeline Integrity
- Never use the same source footage twice unless the storyboard explicitly \
  requires it. Compare each new beat's source + time range against existing \
  beats for overlaps.
- Every storyboard shot must either be in the timeline or explicitly \
  reported as not implemented.
- Preserve the storyboard shot identifier in storyboard_shot.

## Execution Failures

When an execution failure is reported to you (as a tool-role message with \
the failed command, ffmpeg stderr, and beat linkage), analyse the error and \
propose a fix by calling update_edit_plan() with the corrected plan. Common \
fixes: adjust an out-of-range timestamp, change an unsupported transition, \
fix a filter graph argument, or remove a problematic beat. Explain the fix \
to the user in plain English.

## General Rules

- Never invent footage. Only use source videos that were probed.
- If the storyboard references footage that cannot be found, report the \
  problem rather than inventing a clip.
- If something cannot be implemented, report it rather than silently \
  skipping it.
- Honor explicit output-format requests from the user's brief.
- Use the FFmpeg reference (assets/ffmpeg-skill.md) provided in the context \
  to construct correct command arguments.
- Keep your chat responses concise and in plain English. Do not dump raw \
  JSON — summarise what you changed and why.
"""


# --- Pydantic edit-plan models -----------------------------------------------

class TimelineItem(BaseModel):
    """A single beat in the edit timeline (for LEP visual display)."""
    id: str = Field(..., description="Unique shot identifier, e.g. 'shot01_hook'. "
                    "Must match the output_name used in the extract_clip command.")
    source: str = Field(..., description="Source video filename")
    source_start: float = Field(..., ge=0, description="Start time in seconds")
    source_end: float = Field(..., ge=0, description="End time in seconds")
    speed: float = Field(1.0, gt=0, description="Playback speed multiplier")
    transition_out: str | None = Field(None, description="Transition to the next beat")
    transition_duration: float = Field(0.0, ge=0, description="Transition duration in seconds")
    storyboard_shot: str = Field("", description="Storyboard shot reference for traceability")
    storyboard_scene: str = Field("", description="Storyboard scene this shot belongs to")
    purpose: str = Field("", description="Why this shot is used (shown as annotation)")
    description: str = Field("", description="What the shot shows (shown as annotation)")
    intermediate_clip: str = Field("", description="Name of the extracted clip")
    status: str = Field("draft", description="Beat status: draft, approved, needs_attention")
    thumbnail_path: str = Field("", description="Path to the beat thumbnail image")


class TransitionSpec(BaseModel):
    """A transition between two timeline items (legacy/optional — beats now carry transition_out)."""
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


# --- EditCommand tagged union -------------------------------------------------

CommandType = Literal[
    "extract_clip", "create_transition", "create_edit",
    "assemble_timeline", "mix_audio", "render_video", "validate",
]


class EditCommand(BaseModel):
    """A single typed ffmpeg operation in the execution queue.

    The agent constructs these; EditPlanExecutor runs them in order.
    Each command links back to its timeline beat via beat_id (where
    applicable) so the LEP can show which beat a command produces.
    """
    id: str = Field(..., description="Unique command id, e.g. 'cmd01'")
    type: CommandType = Field(..., description="The ffmpeg operation type")
    beat_id: str | None = Field(None, description="Timeline beat this command produces/links to")
    args: dict = Field(default_factory=dict, description="Typed arguments for this command type")
    # Filled by the executor after running:
    status: str = Field("pending", description="pending, running, done, failed, skipped")
    output_path: str = Field("", description="Output file path (set by executor)")
    error: str = Field("", description="Error message if the command failed")


class EditPlan(BaseModel):
    """The complete edit plan: beats (timeline) + commands (ffmpeg ops).

    The agent commits this plan via commit_edit_plan() and amends it via
    update_edit_plan(). The user reviews it in the LEP and runs it via
    EditPlanExecutor.
    """
    version: int = Field(1, ge=1)
    target_duration: float = Field(0.0, ge=0, description="Expected final duration in seconds")
    format: EditFormat = Field(default_factory=EditFormat)
    timeline: list[TimelineItem] = Field(default_factory=list, description="Beats for LEP display")
    commands: list[EditCommand] = Field(default_factory=list, description="FFmpeg ops for execution")
    transitions: list[TransitionSpec] = Field(default_factory=list, description="Legacy transitions (beats carry transition_out)")
    audio: AudioPlan = Field(default_factory=AudioPlan)
    storyboard_version: int = Field(0, description="Storyboard version this edit is based on")
    storyboard_sha: str = Field("", description="Storyboard content hash for traceability")
    output_path: str = Field("", description="Path to the rendered video")
    preset: str = Field("youtube_1080p", description="Render preset used")
    status: str = Field("draft", description="Plan lifecycle: draft → approved → executing → rendered → failed")
    notes: str = Field("", description="Agent's notes about the edit")


# --- Hand-crafted tool schemas for commit/update_edit_plan -------------------
# The ollama SDK flattens nested Pydantic model params to `type: string`,
# so a `plan: EditPlan` hint gives the model NO schema guidance. These
# explicit Tool dicts carry the full nested schema (including the commands
# array) and are passed straight through via Tool.model_validate.

_PLAN_SCHEMA_PROPERTIES = {
    "timeline": {
        "type": "array",
        "description": "Ordered list of beats (shots) in the edit, for visual display.",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique shot id, e.g. 'shot01_hook'. Must match the output_name in the extract_clip command."},
                "source": {"type": "string", "description": "Source video filename"},
                "source_start": {"type": "number", "description": "Start time in seconds"},
                "source_end": {"type": "number", "description": "End time in seconds"},
                "speed": {"type": "number", "description": "Playback speed multiplier (default 1.0)"},
                "storyboard_scene": {"type": "string", "description": "Storyboard scene this shot belongs to"},
                "storyboard_shot": {"type": "string", "description": "Storyboard shot reference"},
                "purpose": {"type": "string", "description": "Why this shot is used (shown as annotation)"},
                "description": {"type": "string", "description": "What the shot shows (shown as annotation)"},
                "transition_out": {"type": "string", "description": "Transition to the next beat (cut, dissolve, ...) or null"},
                "transition_duration": {"type": "number", "description": "Transition duration in seconds"},
            },
            "required": ["id", "source", "source_start", "source_end"],
        },
    },
    "commands": {
        "type": "array",
        "description": "Ordered list of ffmpeg operations to execute. Each command produces or processes a clip.",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Unique command id, e.g. 'cmd01'"},
                "type": {"type": "string", "description": "Operation type: extract_clip, create_transition, create_edit, assemble_timeline, mix_audio, render_video, validate"},
                "beat_id": {"type": "string", "description": "Timeline beat id this command produces (for extract_clip/create_edit) or null"},
                "args": {"type": "object", "description": "Typed arguments for this command type. See system prompt for the schema of each type."},
            },
            "required": ["id", "type", "args"],
        },
    },
    "target_duration": {"type": "number", "description": "Expected final duration in seconds (optional)"},
    "format": {
        "type": "object",
        "description": "Output format (optional)",
        "properties": {
            "width": {"type": "integer", "description": "Output width in pixels"},
            "height": {"type": "integer", "description": "Output height in pixels"},
            "fps": {"type": "number", "description": "Output frame rate"},
        },
    },
    "audio": {
        "type": "object",
        "description": "Audio processing plan (optional)",
        "properties": {
            "volume": {"type": "number", "description": "Master volume multiplier (default 1.0)"},
            "fade_in": {"type": "number", "description": "Fade-in duration in seconds"},
            "fade_out": {"type": "number", "description": "Fade-out duration in seconds"},
            "normalize": {"type": "boolean", "description": "Apply loudnorm normalization"},
        },
    },
    "preset": {"type": "string", "description": "Render preset (default youtube_1080p)"},
}

_EDIT_PLAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "commit_edit_plan",
        "description": (
            "Commit the full Edit Plan (beats + commands) to present it to "
            "the user for review. The plan's timeline is shown visually as "
            "beats; the commands are executed when the user clicks Run. "
            "Each extract_clip command's output_name MUST match a timeline "
            "beat's id so beats are traceable to their clips.\n\n"
            "Example plan:\n"
            '{"timeline": [{"id": "shot01_hook", "source": "GX012053.MP4", '
            '"source_start": 145.0, "source_end": 155.0, "purpose": "Opening hook", '
            '"description": "Boat POV with spray", "transition_out": "dissolve"}], '
            '"commands": [{"id": "cmd01", "type": "extract_clip", "beat_id": "shot01_hook", '
            '"args": {"source": "GX012053.MP4", "start_time": 145.0, "end_time": 155.0, '
            '"output_name": "shot01_hook"}}, {"id": "cmd02", "type": "render_video", '
            '"beat_id": null, "args": {"timeline": "timeline", "output_name": "final.mp4", '
            '"resolution": "1920x1080", "frame_rate": 30, "preset": "youtube_1080p"}}], '
            '"preset": "youtube_1080p"}'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "The complete Edit Plan object.",
                    "properties": _PLAN_SCHEMA_PROPERTIES,
                    "required": ["timeline", "commands"],
                },
            },
            "required": ["plan"],
        },
    },
}

_UPDATE_EDIT_PLAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_edit_plan",
        "description": (
            "Amend the current Edit Plan (e.g. based on user feedback or "
            "after an execution failure). Replaces the current plan "
            "entirely. Pass the complete revised plan. Uses the same plan "
            "structure as commit_edit_plan."
        ),
        "parameters": _EDIT_PLAN_TOOL_SCHEMA["function"]["parameters"],
    },
}


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
    """Holds the 4 planning-only tools and their execution context.

    The agent uses these to probe sources, inspect uncertain boundaries,
    and commit/update the edit plan. FFmpeg execution tools have been
    removed — the EditPlanExecutor runs the queued commands.

    The registry tracks the agent's committed Edit Plan so probe/inspect
    results can inform plan amendments, and so commit/update validate
    against known source durations.
    """

    def __init__(
        self,
        working_folder: str,
        selected_videos: list,
        metadatas: list,
    ) -> None:
        self._working_folder = Path(working_folder)
        self._video_dir = paths.video_dir(self._working_folder)
        self._clips_dir = self._video_dir / CLIPS_SUBDIR
        self._output_dir = self._video_dir / OUTPUT_SUBDIR
        self._preview_dir = self._video_dir / PREVIEW_SUBDIR
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._preview_dir.mkdir(parents=True, exist_ok=True)

        self._source_videos: dict[str, Any] = {v.name: v for v in selected_videos}
        self._metadatas = {m.source_filename: m for m in metadatas}
        self._current_plan: EditPlan | None = None

    @property
    def current_plan(self) -> "EditPlan | None":
        """The latest committed/updated edit plan (or None if not yet committed)."""
        return self._current_plan

    def get_tools(self) -> list[Callable]:
        """Return the full list of tool functions for dispatch."""
        return [
            self.probe_video,
            self.inspect_clip,
            self.commit_edit_plan,
            self.update_edit_plan,
        ]

    def get_chat_tools(self) -> list:
        """Schemas passed to client.chat for the model to call.

        probe_video and inspect_clip are callables (the SDK derives their
        schemas). The two plan tools are passed as explicit Tool dicts
        because the SDK flattens nested Pydantic model params to
        `type: string`, which would give the model no schema for the plan
        structure. The callables are still used for dispatch (matched by
        __name__).
        """
        return [
            self.probe_video,
            self.inspect_clip,
            dict(_EDIT_PLAN_TOOL_SCHEMA),
            dict(_UPDATE_EDIT_PLAN_TOOL_SCHEMA),
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

    # --- Tool 3: commit_edit_plan -------------------------------------------

    def commit_edit_plan(self, plan: dict) -> dict:
        """Commit the structured Edit Plan (beats + commands).

        This presents the plan to the user for review in the LEP. The
        commands are not executed until the user clicks Run.

        Args:
            plan: The complete Edit Plan as a JSON object. Must have
                timeline (beats) and commands (ffmpeg ops).
        """
        return self._store_plan(plan, "draft")

    # --- Tool 4: update_edit_plan -------------------------------------------

    def update_edit_plan(self, plan: dict) -> dict:
        """Amend the current Edit Plan based on user feedback or execution failure.

        Replaces the current plan entirely. Re-persists to disk.

        Args:
            plan: The complete revised Edit Plan as a JSON object.
        """
        return self._store_plan(plan, "draft")

    # --- Internal helpers ----------------------------------------------------

    def _store_plan(self, plan: dict, default_status: str) -> dict:
        """Validate, store, and persist an Edit Plan. Returns a tool message."""
        start = time.time()
        if not isinstance(plan, dict):
            return ToolResult(
                False,
                {"error": "plan must be a JSON object"},
                log="commit_edit_plan: invalid plan",
                duration_s=time.time() - start,
            ).to_tool_message()
        try:
            parsed = EditPlan.model_validate(plan)
        except ValidationError as e:
            return ToolResult(
                False,
                {"error": f"Invalid edit plan: {e}"},
                log="commit_edit_plan: validation failed",
                duration_s=time.time() - start,
            ).to_tool_message()

        warnings = self._validate_plan_consistency(parsed)

        if not parsed.status or parsed.status == "draft":
            parsed.status = default_status
        self._current_plan = parsed
        self._persist_current_plan()
        result_data = {
            "plan": parsed.model_dump(),
            "status": parsed.status,
            "timeline_count": len(parsed.timeline),
            "commands_count": len(parsed.commands),
        }
        if warnings:
            result_data["warnings"] = warnings
            result_data["note"] = (
                "Plan accepted but has issues that should be corrected "
                "before execution (e.g. via update_edit_plan)."
            )
        return ToolResult(
            True,
            result_data,
            log=f"commit_edit_plan: {len(parsed.timeline)} beats, "
                f"{len(parsed.commands)} commands, status={parsed.status}"
                + (f", {len(warnings)} warnings" if warnings else ""),
            duration_s=time.time() - start,
        ).to_tool_message()

    def _validate_plan_consistency(self, plan: EditPlan) -> list[str]:
        """Check plan timeline items against known source durations."""
        issues: list[str] = []
        seen_ranges: dict[str, list[tuple[float, float]]] = {}
        ids_seen: set[str] = set()
        for item in plan.timeline:
            if item.id in ids_seen:
                issues.append(f"Duplicate beat id '{item.id}'")
            ids_seen.add(item.id)

            if item.source not in self._source_videos:
                issues.append(
                    f"Beat '{item.id}': unknown source '{item.source}'. "
                    f"Available: {list(self._source_videos.keys())}"
                )
            else:
                meta = self._metadatas.get(item.source)
                dur = meta.duration if meta else 0.0
                if dur > 0:
                    if item.source_start < 0 or item.source_end > dur:
                        issues.append(
                            f"Beat '{item.id}': range {item.source_start}-{item.source_end} "
                            f"exceeds source '{item.source}' duration {dur}s"
                        )
                if item.source_start >= item.source_end:
                    issues.append(
                        f"Beat '{item.id}': start ({item.source_start}) >= "
                        f"end ({item.source_end})"
                    )
                ranges = seen_ranges.setdefault(item.source, [])
                for (s, e) in ranges:
                    if item.source_start < e and item.source_end > s:
                        issues.append(
                            f"Beat '{item.id}': overlaps existing beat on "
                            f"'{item.source}' ({s}-{e})"
                        )
                        break
                ranges.append((item.source_start, item.source_end))

        # Check that extract_clip commands reference known sources
        for cmd in plan.commands:
            if cmd.type == "extract_clip":
                src = cmd.args.get("source", "")
                if src and src not in self._source_videos:
                    issues.append(
                        f"Command '{cmd.id}': extract_clip references unknown "
                        f"source '{src}'"
                    )
        return issues

    def _persist_current_plan(self) -> None:
        """Write self._current_plan to .llama-cut/video/edit_plan.json."""
        if self._current_plan is None:
            return
        try:
            self._video_dir.mkdir(parents=True, exist_ok=True)
            (self._video_dir / EDIT_PLAN_FILENAME).write_text(
                self._current_plan.model_dump_json(indent=2), encoding="utf-8",
            )
        except OSError:
            pass


# --- Prompt building ---------------------------------------------------------

def build_chat_system_context(storyboard_md: str, context_md: str,
                              ffmpeg_skill_md: str) -> str:
    """Build the context injected as the first user message in the chat.

    Contains the storyboard, assembled context, and the FFmpeg skill
    reference so the agent can construct correct commands.
    """
    return (
        f"## Storyboard\n\n{storyboard_md.strip()}\n\n"
        f"## Available Context\n\n{context_md.strip()}\n\n"
        f"## FFmpeg Reference\n\n{ffmpeg_skill_md.strip()}\n\n"
        f"## Your Task\n\n"
        f"Help the user build an edit plan from the storyboard above. "
        f"Use the FFmpeg reference to construct correct command arguments. "
        f"Probe sources before referencing their timestamps. When the chat "
        f"is empty, greet the user and offer conversation starters."
    )


# --- Persistence -------------------------------------------------------------

def _video_dir(working_folder: str) -> Path:
    return paths.video_dir(working_folder)


def save_edit_plan(working_folder: str, plan: EditPlan) -> Path:
    """Persist the edit plan to .llama-cut/video/edit_plan.json."""
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
    """Persist the tool execution log to .llama-cut/video/tool_log.json."""
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


def save_chat(working_folder: str, messages: list[dict]) -> Path:
    """Persist the chat transcript to .llama-cut/video/chat.json."""
    d = _video_dir(working_folder)
    d.mkdir(parents=True, exist_ok=True)
    p = d / CHAT_FILENAME
    p.write_text(json.dumps(messages, indent=2), encoding="utf-8")
    return p


def load_chat(working_folder: str) -> list[dict]:
    """Load the chat transcript. Returns [] if not found."""
    p = _video_dir(working_folder) / CHAT_FILENAME
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def clear_production(working_folder: str) -> None:
    """Delete all video production artefacts.

    Removes the entire <working_folder>/.llama-cut/video/ directory.
    Does not raise if the directory does not exist.
    """
    d = _video_dir(working_folder)
    if not d.exists():
        return
    shutil.rmtree(d, ignore_errors=True)


def find_rendered_video(working_folder: str) -> Path | None:
    """Find the most recently rendered final video in .llama-cut/video/output/.

    Returns the newest .mp4 path (by modification time), or None if the
    directory is empty or missing.
    """
    out_dir = _video_dir(working_folder) / OUTPUT_SUBDIR
    if not out_dir.exists():
        return None
    videos = sorted(
        out_dir.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return videos[0] if videos else None


# --- Beat thumbnails ---------------------------------------------------------

def extract_beat_thumbnail(working_folder: str, source_path: str,
                           source_start: float, source_end: float,
                           beat_id: str, width: int = 320) -> str:
    """Extract a single thumbnail frame at the midpoint of a beat's range.

    Caches the result as <beat_id>.jpg in .llama-cut/video/thumbs/.
    Returns the path to the thumbnail, or "" on failure.
    """
    d = _video_dir(working_folder) / THUMBS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    safe_id = _sanitize_name(beat_id)
    out_path = d / f"{safe_id}.jpg"
    if out_path.exists():
        return str(out_path)

    midpoint = (source_start + source_end) / 2.0
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-y",
        "-ss", str(midpoint),
        "-i", source_path,
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "3",
        str(out_path),
    ]
    rc, _stdout, _stderr = _run_ffmpeg(cmd, timeout=30)
    if rc != 0 or not out_path.exists():
        return ""
    return str(out_path)


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


def load_ffmpeg_skill() -> str:
    """Load the FFmpeg skill reference from assets/ffmpeg-skill.md."""
    skill_path = Path(__file__).resolve().parent.parent / "assets" / "ffmpeg-skill.md"
    if not skill_path.exists():
        return ""
    try:
        return skill_path.read_text(encoding="utf-8")
    except OSError:
        return ""