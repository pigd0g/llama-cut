"""Stage 8 — Final Video Production engine.

Pure-logic module containing:
  * Ollama configuration (reuses OLLAMA_WORKFLOW_MODEL)
  * Pydantic edit-plan models with storyboard→source traceability
  * ToolRegistry implementing domain-specific FFmpeg tools
  * Two-phase agent loop: plan-then-execute, with amendable edit plan
  * Persistence (edit plan, tool log, clear)

The LLM decides *what* should happen. Python decides *how* to safely
execute it. FFmpeg performs the actual media operations.

Stage 8 V1 philosophy
---------------------

Goal: accurate → deterministic → inspectable → recoverable.

The editor is a careful editor, not a fast editor. The agent must first
translate the storyboard into a structured Edit Plan, then execute that
plan with full per-stage validation. Intermediate clips are kept on disk
as checkpoints (shot01_hook.mp4, shot02a_ducks.mp4, ...) so failures can
be localised to an exact step.

Priority order (enforced via the system prompt):
  1. Correctly interpret the storyboard
  2. Select the correct source footage
  3. Respect exact timestamps
  4. Produce the intended sequence
  5. Apply transitions/effects correctly
  6. Verify the resulting video
  7. Only then optimise rendering performance

Explicitly deferred (do NOT implement in V1):
  - Proxy / draft-resolution workflows
  - Aggressive intermediate caching
  - Single-pass filter graphs
  - Render optimisation beyond preset selection
  - GPU / NVENC tuning beyond the current auto-use fallback
  - Parallel rendering
  - Intermediate-file elimination

Once several edits have been produced end-to-end correctly, the expensive
parts can be optimised without changing the underlying edit decisions.
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

from . import paths


# --- Constants ---------------------------------------------------------------

MAX_AGENT_ROUND_TRIPS = 150

# Leaf directory name (kept for tests that import the constant). The actual
# path is resolved through ``paths.video_dir()`` so it lands under
# ``.llama-cut/``.
VIDEO_DIR = paths.VIDEO_DIR_NAME
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

You are a CAREFUL editor, not a fast editor. Prioritise correctness and \
inspectability over speed. Intermediate clips are kept on disk as \
checkpoints (shot01_hook.mp4, shot02a_ducks.mp4, ...) so a failure can be \
localised to an exact step. Do not optimise for render speed.

## Priority Order

Work strictly in this priority order. Never trade a higher priority for a \
lower one:

  1. Correctly interpret the storyboard
  2. Select the correct source footage
  3. Respect exact timestamps
  4. Produce the intended sequence
  5. Apply transitions/effects correctly
  6. Verify the resulting video
  7. Only then optimise rendering performance

## Two-Phase Workflow

### Phase 1 — Plan

Before touching any footage, translate the storyboard into a structured \
Edit Plan. In this phase you may ONLY use the tools: probe_video, \
inspect_clip, commit_edit_plan, and update_edit_plan.

  Phase 1A — Understand the sources:
    Call probe_video() for every source video referenced in the storyboard. \
Review the returned metadata (duration, resolution, fps, codec, audio).

  Phase 1B — Verify uncertain boundaries:
    The frame analysis in the context is based on representative frames, \
not exact boundaries. When a storyboard moment's exact start/end is not \
certain, call inspect_clip() on the source to extract representative \
frames from the candidate range and reason about their count and \
timestamps. NOTE: visual content verification is not available in this \
pass — rely on frame count, timestamps, and probed metadata.

  Phase 1C — Commit the plan:
    Call commit_edit_plan() with a complete structured Edit Plan. Each \
timeline item must include: id, storyboard_scene, source (exact filename), \
source_start, source_end, purpose, description, and a clip id that matches \
the output_name you will use in extract_clip (e.g. 'shot01_hook'). The plan \
is the source of truth for the edit.

    The plan argument is a JSON object with this structure:
    {
      "timeline": [
        {
          "id": "shot01_hook",
          "source": "GX012053.MP4",
          "source_start": 145.0,
          "source_end": 155.0,
          "speed": 1.0,
          "storyboard_scene": "Act 1 - 0:00-0:10",
          "purpose": "Opening hook",
          "description": "Boat POV with spray"
        },
        {
          "id": "shot02a_boy",
          "source": "GX012054.MP4",
          "source_start": 0.0,
          "source_end": 7.0,
          "storyboard_scene": "Act 1 - 0:10-0:17",
          "purpose": "Kid moment"
        }
      ],
      "target_duration": 178.0,
      "transitions": [
        {"after": "shot01_hook", "type": "dissolve", "duration": 1.5}
      ]
    }
    Only `timeline` is required. Optional fields (target_duration, \
transitions, audio, preset) default sensibly. The `timeline` MUST be a JSON \
array of shot objects — never nest it under a key like `item`, `shots`, or \
`metadata`. Do not include transition-only entries (source: "internal") in \
the timeline; describe transitions in the separate `transitions` array.

If inspect_clip later reveals that a boundary must change, call \
update_edit_plan() with the corrected plan. You may also call \
update_edit_plan() during execution if a tool result shows a range is wrong.

### Phase 2 — Execute

Once commit_edit_plan() has been called, all tools are available. Execute \
the committed plan. The clip id from each timeline item becomes the \
output_name passed to extract_clip(), so every clip is traceable to its \
plan entry.

The execute flow is:

  Storyboard + Context
        ↓
  Inspect / Probe footage        (probe_video, inspect_clip)
        ↓
  Create individual clips         (extract_clip)
        ↓
  Inspect / validate clips        (validate_clip)
        ↓
  Apply transitions / effects    (create_edit, create_transition, mix_audio)
        ↓
  Assemble timeline               (assemble_timeline)
        ↓
  Render                          (render_video)
        ↓
  Verify output                   (validate_video)
        ↓
  Result

## Accuracy Rules

### Timestamps
- NEVER assume a timestamp. Before extracting from a source, call \
probe_video() for that source and read its real duration.
- Before extract_clip(), validate: start >= 0, end <= duration, start < end.
- If a timestamp is invalid, RESOLVE the problem (inspect to find the \
correct boundary, or report the gap). NEVER silently clip or shift a \
range to make it valid.
- A storyboard timestamp that falls outside the source duration is a \
problem to report, not a range to truncate.

### Inspect before extract
- When the storyboard says "GX012053.MP4 02:25–02:30" and the exact \
boundaries may need refinement, call inspect_clip("GX012053.MP4", 145, 150) \
to confirm the range before extracting. Frame analysis is representative, \
not exact, so boundary refinement is often necessary.

### Traceability
- Every generated clip must carry: storyboard scene, source filename, \
source start, source end, generated clip name, description.
- Use the clip id from the committed plan as the output_name in \
extract_clip() so each clip maps back to its plan entry.
- Name clips after shots for debuggability: shot01_hook, shot02a_ducks, \
shot02b_boat, shot03_family, ...

### Validate every stage
- Do NOT rely on FFmpeg exit code 0 alone.
- After creating each clip: call validate_clip() on it. Check duration, \
resolution, frame rate, audio presence, file existence. If the actual \
duration does not match (end - start), re-extract or correct the range.
- After assembling: call validate_video() on the assembled timeline.
- After final render: call validate_video() on the final output.
- If validation fails, read the error, correct the issue, and re-render.

### Transitions
- Keep create_transition() as a separate step. Do NOT optimise it away. \
This is intentional: keeping transition clips on disk as separate files \
(transition01.mp4) makes the edit inspectable and recoverable.
- Transitions only between exactly two clips.

### Assembly
- Pass ONLY shot clips to assemble_timeline() in the `clips` list. Do NOT \
include transition clips (created by create_transition) in `clips`` — \
describe transitions in the separate `transitions` parameter.
- The `transitions` array uses `after` to identify which shot a transition \
follows; the transition is applied between that shot and the next shot in \
the `clips` list. Match `after` to the shot's `output_name`, not its \
storyboard id.
- If assemble_timeline fails, read the error and simplify: reduce the \
clips list to shot clips only, ensure all clip names exist on disk, and \
verify transition `after` values match clip names in the list.

### Background Music
- Audio files (e.g. .mp3, .wav, .m4a) placed in the project folder can be \
mixed in as background music via mix_audio(). Pass the music filename in the \
`audio_sources` list.
- The available music files are listed in the provided context under \
"Available Music Files". Reference a music file by its exact filename.
- Only use music the user has explicitly requested. Do not add background \
music unless the storyboard or user brief calls for it.

## General Rules

- Never invent footage. Only use source videos that were probed.
- If the storyboard references footage that cannot be found, report the \
problem rather than inventing a clip.
- If something cannot be implemented, report it rather than silently \
skipping it.
- Always probe a video before extracting clips from it.
- Verify timestamps are within the video duration before extracting.
- Use unique output_name values for every intermediate clip.
- Source video files are read-only — never attempt to modify them.
- Do not write files outside the project working directory.
- If a tool returns an error, read the error message and correct your \
approach.

### Timeline Integrity

- Source footage usage must be tracked by source file AND time range, \
not just by filename.
- When selecting a new clip, inspect the existing timeline for previously \
used ranges from the same source.
- A source file being different does not make footage unique; uniqueness \
is determined by the source file plus its timestamp range.
- Never use the same source footage twice unless the storyboard explicitly \
requires the repetition.
- Never create two timeline shots whose source time ranges overlap, unless \
the storyboard explicitly requires the overlap.
- Before adding a shot to the timeline, compare its source file and \
source_start/source_end against every existing timeline shot.
- If a new shot overlaps an existing shot from the same source, either \
adjust the new range, reuse the existing shot, or report the conflict. Do \
not silently create the overlap.
- Reusing the same source video at different, non-overlapping timestamps \
is allowed.
- A longer shot must not contain footage already used by an earlier shot \
unless that repetition is explicitly intentional.
- Treat the structured timeline (the Edit Plan) as the source of truth. \
The final edit summary must describe the actual timeline and must not \
contain shots, transitions, durations, or creative decisions that are \
not represented in the timeline.
- Every timeline shot must correspond to a specific storyboard shot or an \
explicitly justified editorial insertion.
- Preserve the storyboard shot identifier in storyboard_shot for every \
timeline shot. Never leave storyboard_shot empty when the shot originated \
from the storyboard.
- Do not create multiple timeline shots for the same storyboard shot unless \
the storyboard explicitly requires it or the split is necessary to \
implement the storyboard.
- Every storyboard shot that is required by the storyboard must either be \
represented in the timeline or be explicitly reported as not implemented.
- Do not silently omit storyboard shots.
- Do not silently duplicate storyboard shots.
- Before finalising the timeline, perform a complete validation pass for \
duplicate footage, overlapping source ranges, missing storyboard shots, \
and invalid timeline references.

### Transition Integrity

- Every transition must reference valid adjacent timeline shots.
- A transition must identify the correct preceding and following shots; \
never attach multiple unrelated transitions to the same shot.
- Do not create transitions that are not represented in the structured \
timeline.
- The transition definitions and the final edit summary must agree with \
the actual timeline.
- Validate transition ordering and placement before finalising the edit.

## Deferred Optimisations (do NOT implement in V1)

Do not attempt: proxy / draft-resolution workflows, aggressive \
intermediate caching, single-pass filter graphs, render optimisation \
beyond preset selection, GPU tuning beyond the automatic NVENC fallback, \
parallel rendering, or intermediate-file elimination. Focus on accuracy.

## Output

When you have finished rendering and validating the video, respond with a \
summary of the final video (duration, resolution, file path, and any notes \
about creative decisions made during editing). The summary must agree \
with the committed Edit Plan and the actual rendered output.
"""


REFINEMENT_INSTRUCTIONS = """\
You are refining an existing video edit based on user feedback.

The user has provided feedback on the current edit. Apply the requested \
changes while preserving good decisions from the existing edit.

Use the supplied storyboard, context, and current edit plan to make \
targeted modifications. You do not need to rebuild the entire edit from \
scratch — modify only what the user has requested.

Treat the supplied existing edit plan as your starting point. Amend it \
with commit_edit_plan() / update_edit_plan() to reflect only the requested \
changes, then execute only the affected shots. Reuse existing intermediate \
clips that are not affected by the changes — a clip that already exists on \
disk with the same name can be reused without re-extracting it.

Follow the same two-phase workflow (plan → execute) and the same accuracy, \
traceability, and validation rules as a fresh edit. Reuse of unaffected \
intermediate clips is the only optimisation permitted in V1.
"""


# --- Pydantic edit-plan models (per spec §20) --------------------------------

class TimelineItem(BaseModel):
    """A single shot in the edit timeline."""
    id: str = Field(..., description="Unique shot identifier, e.g. 'shot01_hook'. "
                    "Must match the output_name used in extract_clip.")
    source: str = Field(..., description="Source video filename")
    source_start: float = Field(..., ge=0, description="Start time in seconds")
    source_end: float = Field(..., ge=0, description="End time in seconds")
    speed: float = Field(1.0, gt=0, description="Playback speed multiplier")
    transition_in: str | None = Field(None, description="Transition into this clip")
    transition_out: str | None = Field(None, description="Transition out of this clip")
    storyboard_shot: str = Field("", description="Storyboard shot reference for traceability")
    storyboard_scene: str = Field("", description="Storyboard scene this shot belongs to")
    purpose: str = Field("", description="Why this shot is used (e.g. 'Opening hook')")
    description: str = Field("", description="What the shot shows (e.g. 'Family jumps into water')")
    intermediate_clip: str = Field("", description="Name/path of the extracted/processed clip")


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
    """The complete machine-readable edit plan.

    The agent commits this plan via commit_edit_plan() before executing any
    clips, and amends it via update_edit_plan() when boundaries change.
    """
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
    status: str = Field("draft", description="Plan lifecycle: draft → executing → rendered → verified")
    notes: str = Field("", description="Agent's notes about the edit")


# --- Hand-crafted tool schemas for commit_edit_plan / update_edit_plan --------
# The ollama SDK (0.6.x) flattens nested Pydantic model params to `type: string`
# via convert_function_to_tool, so a `plan: EditPlan` hint gives the model NO
# schema guidance. These explicit Tool dicts carry the full nested schema and
# are passed straight through via Tool.model_validate, bypassing the flattening.
# The callable is still used for dispatch (matched by __name__).

_EDIT_PLAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "commit_edit_plan",
        "description": (
            "Commit the structured Edit Plan before executing any clips. This "
            "is Phase 1 of the workflow. The plan becomes the source of truth "
            "for the edit. Each timeline item's id MUST match the output_name "
            "you will pass to extract_clip() so every clip is traceable to its "
            "plan entry. Only required fields need be supplied; optional "
            "fields default sensibly.\n\n"
            "Example plan:\n"
            '{"timeline": [{"id": "shot01_hook", "source": "GX012053.MP4", '
            '"source_start": 145.0, "source_end": 155.0, "speed": 1.0, '
            '"storyboard_scene": "Act 1", "purpose": "Opening hook", '
            '"description": "Boat POV with spray"}, {"id": "shot02a_boy", '
            '"source": "GX012054.MP4", "source_start": 0.0, '
            '"source_end": 7.0, "storyboard_scene": "Act 1", '
            '"purpose": "Kid moment"}], "target_duration": 178.0, '
            '"transitions": [{"after": "shot01_hook", "type": "dissolve", '
            '"duration": 1.5}]}'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "The complete Edit Plan object.",
                    "properties": {
                        "timeline": {
                            "type": "array",
                            "description": "Ordered list of shots in the edit.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "Unique shot id, e.g. 'shot01_hook'. Must match the output_name used in extract_clip."},
                                    "source": {"type": "string", "description": "Source video filename"},
                                    "source_start": {"type": "number", "description": "Start time in seconds"},
                                    "source_end": {"type": "number", "description": "End time in seconds"},
                                    "speed": {"type": "number", "description": "Playback speed multiplier (default 1.0)"},
                                    "storyboard_scene": {"type": "string", "description": "Storyboard scene this shot belongs to"},
                                    "purpose": {"type": "string", "description": "Why this shot is used"},
                                    "description": {"type": "string", "description": "What the shot shows"},
                                    "transition_in": {"type": "string", "description": "Transition into this clip (optional)"},
                                    "transition_out": {"type": "string", "description": "Transition out of this clip (optional)"},
                                },
                                "required": ["id", "source", "source_start", "source_end"],
                            },
                        },
                        "transitions": {
                            "type": "array",
                            "description": "Transitions between adjacent timeline shots.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "after": {"type": "string", "description": "Clip id this transition follows"},
                                    "type": {"type": "string", "description": "Transition type (cut, dissolve, fadeblack, fadewhite, wipeleft, etc.)"},
                                    "duration": {"type": "number", "description": "Transition duration in seconds"},
                                },
                                "required": ["after", "type"],
                            },
                        },
                        "target_duration": {"type": "number", "description": "Expected final duration in seconds (optional)"},
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
                    },
                    "required": ["timeline"],
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
            "Amend the committed Edit Plan (e.g. after inspect_clip reveals a "
            "boundary correction, or a tool result shows a range is wrong). "
            "Re-persists the plan. Pass the complete revised plan. Uses the "
            "same plan structure as commit_edit_plan."
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
    """Holds the domain-specific tools and their execution context.

    Each tool is a method with type hints + docstring. The Ollama SDK derives
    tool schemas from these. Tools validate all inputs before executing FFmpeg.

    The registry also tracks the agent's committed Edit Plan (via
    commit_edit_plan / update_edit_plan) so that extract_clip can link each
    realised clip back to its plan entry for traceability.
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
        self._intermediate_clips: dict[str, str] = {}
        self._current_plan: EditPlan | None = None

    @property
    def current_plan(self) -> "EditPlan | None":
        """The latest committed/updated edit plan (or None if not yet committed)."""
        return self._current_plan

    def get_tools(self) -> list[Callable]:
        """Return the full list of tool functions for the Ollama SDK."""
        return [
            self.probe_video,
            self.inspect_clip,
            self.extract_clip,
            self.create_edit,
            self.create_transition,
            self.mix_audio,
            self.assemble_timeline,
            self.render_video,
            self.validate_clip,
            self.validate_video,
            self.commit_edit_plan,
            self.update_edit_plan,
        ]

    def get_plan_phase_tools(self) -> list[Callable]:
        """Tools allowed during Phase 1 (planning) — before clips exist."""
        return [
            self.probe_video,
            self.inspect_clip,
            self.commit_edit_plan,
            self.update_edit_plan,
        ]

    def get_chat_tools(self) -> list:
        """Schemas passed to client.chat for the model to call.

        Most tools are callables (the SDK derives their schemas). The two
        plan tools are passed as explicit Tool dicts because the SDK flattens
        nested Pydantic model params to `type: string`, which would give the
        model no schema for the plan structure. The callables in get_tools()
        are still used for dispatch (matched by __name__).
        """
        # Use a shallow copy of the dict so the SDK doesn't mutate the module
        # constant when it serializes it.
        return [
            self.probe_video,
            self.inspect_clip,
            self.extract_clip,
            self.create_edit,
            self.create_transition,
            self.mix_audio,
            self.assemble_timeline,
            self.render_video,
            self.validate_clip,
            self.validate_video,
            dict(_EDIT_PLAN_TOOL_SCHEMA),
            dict(_UPDATE_EDIT_PLAN_TOOL_SCHEMA),
        ]

    def get_plan_phase_chat_tools(self) -> list:
        """Schemas passed to client.chat during Phase 1 (planning)."""
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

        # Probe the newly created clip so the agent gets immediate feedback
        # on actual duration / resolution / audio and can detect a wrong range
        # (e.g. actual_duration != end_time - start_time) and re-extract.
        actual_duration = 0.0
        actual_resolution = "unknown"
        audio_present = False
        probe = None
        try:
            from .ffmpeg.probe import run_ffprobe as _run_probe
            probe = _run_probe(str(out_path))
        except Exception:
            probe = None
        if probe is not None:
            actual_duration = probe.duration
            actual_resolution = f"{probe.width}x{probe.height}" if probe.width else "unknown"
            # ProbeResult parses only the video stream; check audio from raw.
            for stream in (probe.raw or {}).get("streams", []):
                if stream.get("codec_type") == "audio":
                    audio_present = True
                    break

        # Link this realised clip back to its committed plan entry (if any)
        # for traceability, then re-persist the plan.
        linked_scene = ""
        if self._current_plan is not None:
            for item in self._current_plan.timeline:
                if item.id == safe_name:
                    item.intermediate_clip = safe_name
                    linked_scene = item.storyboard_scene
                    break
            if linked_scene:
                self._persist_current_plan()

        result_data = {
            "output_name": safe_name,
            "output_path": str(out_path),
            "source": fname,
            "start_time": start_time,
            "end_time": end_time,
            "duration": end_time - start_time,
            "actual_duration": round(actual_duration, 3),
            "actual_resolution": actual_resolution,
            "audio_present": audio_present,
        }
        if linked_scene:
            result_data["linked_storyboard_scene"] = linked_scene
        return ToolResult(
            True,
            result_data,
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
            audio_sources: List of audio source names. These may be
                intermediate clips OR background-music files from the project
                folder (e.g. 'background.mp3'). Reference music files by their
                exact filename.
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

        `clips` is the ordered list of SHOT clips to concatenate. Do NOT
        include transition clips (created by create_transition) in this list
        — transitions are described purely in the `transitions` parameter
        and applied between adjacent shots. If you already created
        transition clips, pass the original shot clips here and describe
        the transitions in the `transitions` array.

        Args:
            clips: Ordered list of shot clip names (no transition clips).
            transitions: List of transition specs with 'after', 'type',
                'duration'. Each 'after' is the clip id that the transition
                follows; the transition is applied between that clip and the
                next clip in the `clips` list.
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
                        expected_resolution: str | None = None,
                        expected_fps: float | None = None,
                        require_audio: bool = False) -> dict:
        """Validate a rendered or assembled video file using ffprobe.

        Resolves the path across the output, clips, and preview directories
        so it can validate the assembled timeline (in clips/) and the final
        render (in output/).

        Args:
            video_path: Path or name of the video to validate.
            expected_duration: Optional expected duration in seconds.
            expected_resolution: Optional expected resolution as WxH.
            expected_fps: Optional expected frame rate.
            require_audio: If true, fail when no audio stream is present.
        """
        start = time.time()
        p = self._resolve_any_video(video_path)
        if p is None:
            return ToolResult(
                False,
                {"error": f"File does not exist: {video_path}"},
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

        audio_present = self._has_audio_stream(result.raw)
        file_size = 0
        try:
            file_size = p.stat().st_size
        except OSError:
            pass

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
        if expected_fps and abs(result.fps - expected_fps) > 0.5:
            issues.append(
                f"Frame rate mismatch: expected {expected_fps}fps, got {result.fps}fps"
            )
        if require_audio and not audio_present:
            issues.append("Required audio stream is missing")
        if file_size == 0:
            issues.append("File is empty (0 bytes)")

        passed = len(issues) == 0
        data = {
            "passed": passed,
            "duration": result.duration,
            "resolution": f"{result.width}x{result.height}",
            "codec": result.codec,
            "fps": result.fps,
            "audio_present": audio_present,
            "file_size": file_size,
            "issues": issues,
            "path": str(p),
        }
        return ToolResult(
            passed, data,
            log=f"validate_video: {'PASS' if passed else 'FAIL: ' + '; '.join(issues)}",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 10: validate_clip ---------------------------------------------

    def validate_clip(self, clip_name: str,
                      expected_duration: float | None = None,
                      expected_resolution: str | None = None) -> dict:
        """Validate an intermediate clip in the clips directory using ffprobe.

        Use this after extract_clip() / create_edit() to verify each clip:
        duration, resolution, frame rate, audio presence, file existence.
        If actual_duration does not match expected_duration, the range was
        likely wrong and the clip should be re-extracted.

        Args:
            clip_name: Name of the clip (with or without extension).
            expected_duration: Optional expected duration in seconds.
            expected_resolution: Optional expected resolution as WxH.
        """
        start = time.time()
        p = self._resolve_clip(Path(clip_name).stem)
        if p is None:
            return ToolResult(
                False,
                {"error": f"Clip '{clip_name}' not found in clips directory"},
                log="validate_clip: clip not found",
                duration_s=time.time() - start,
            ).to_tool_message()

        from .ffmpeg.probe import run_ffprobe
        result = run_ffprobe(str(p))
        if result is None:
            return ToolResult(
                False,
                {"error": "ffprobe failed — clip may be corrupt or unplayable"},
                log="validate_clip: ffprobe failed",
                duration_s=time.time() - start,
            ).to_tool_message()

        audio_present = self._has_audio_stream(result.raw)
        file_size = 0
        try:
            file_size = p.stat().st_size
        except OSError:
            pass

        issues: list[str] = []
        if result.duration <= 0:
            issues.append("Zero or negative duration")
        if expected_duration and abs(result.duration - expected_duration) > 0.5:
            issues.append(
                f"Duration mismatch: expected {expected_duration}s, "
                f"got {result.duration}s"
            )
        if expected_resolution:
            exp_w, exp_h = expected_resolution.split("x")
            if result.width != int(exp_w) or result.height != int(exp_h):
                issues.append(
                    f"Resolution mismatch: expected {expected_resolution}, "
                    f"got {result.width}x{result.height}"
                )
        if file_size == 0:
            issues.append("File is empty (0 bytes)")

        passed = len(issues) == 0
        data = {
            "passed": passed,
            "clip": p.name,
            "duration": result.duration,
            "resolution": f"{result.width}x{result.height}",
            "fps": result.fps,
            "audio_present": audio_present,
            "file_size": file_size,
            "issues": issues,
            "path": str(p),
        }
        return ToolResult(
            passed, data,
            log=f"validate_clip: {p.name} {'PASS' if passed else 'FAIL: ' + '; '.join(issues)}",
            duration_s=time.time() - start,
        ).to_tool_message()

    # --- Tool 11: commit_edit_plan -------------------------------------------

    def commit_edit_plan(self, plan: dict) -> dict:
        """Commit the structured Edit Plan before executing any clips.

        This is Phase 1 of the workflow. The plan becomes the source of
        truth for the edit. Each timeline item's id MUST match the
        output_name you will pass to extract_clip() so every clip is
        traceable to its plan entry.

        After commit_edit_plan() is called, the full tool set becomes
        available for Phase 2 (execution).

        Args:
            plan: The complete Edit Plan as a JSON object. Must validate
                against the EditPlan schema. Each timeline item needs:
                id, source, source_start, source_end. Optional but
                recommended: storyboard_scene, purpose, description.
        """
        return self._store_plan(plan, "draft")

    # --- Tool 12: update_edit_plan -------------------------------------------

    def update_edit_plan(self, plan: dict) -> dict:
        """Amend the committed Edit Plan (e.g. after inspect_clip reveals a
        boundary correction, or a tool result shows a range is wrong).

        Re-persists the plan. The plan remains the source of truth.

        Args:
            plan: The complete revised Edit Plan as a JSON object.
        """
        return self._store_plan(plan, "executing")

    # --- Internal helpers ----------------------------------------------------

    def _store_plan(self, plan: dict, default_status: str) -> dict:
        """Validate, store, and persist an Edit Plan. Returns a tool message.

        Hard schema errors (Pydantic validation failures) reject the plan.
        Consistency issues (unknown source, out-of-range timestamp, overlap)
        do NOT reject — the plan is accepted and the issues are returned as
        warnings so the agent can correct them during execution. This avoids
        a retry loop where the agent keeps resubmitting the plan.
        """
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

        # Check plan-level consistency against probed source durations.
        # These are warnings, not hard errors — accept the plan and let the
        # agent fix issues during execution (e.g. via inspect_clip).
        warnings = self._validate_plan_consistency(parsed)

        if not parsed.status or parsed.status == "draft":
            parsed.status = default_status
        self._current_plan = parsed
        self._persist_current_plan()
        result_data = {
            "plan": parsed.model_dump(),
            "status": parsed.status,
            "timeline_count": len(parsed.timeline),
        }
        if warnings:
            result_data["warnings"] = warnings
            result_data["note"] = (
                "Plan accepted but has issues that should be corrected during "
                "execution (e.g. via inspect_clip or update_edit_plan)."
            )
        return ToolResult(
            True,
            result_data,
            log=f"commit_edit_plan: {len(parsed.timeline)} shots, status={parsed.status}"
                + (f", {len(warnings)} warnings" if warnings else ""),
            duration_s=time.time() - start,
        ).to_tool_message()

    def _validate_plan_consistency(self, plan: EditPlan) -> list[str]:
        """Check plan timeline items against known source durations.

        Returns a list of human-readable issue strings (empty if consistent).
        Unknown sources and out-of-range timestamps are reported so the agent
        can correct them before execution.
        """
        issues: list[str] = []
        seen_ranges: dict[str, list[tuple[float, float]]] = {}
        ids_seen: set[str] = set()
        for item in plan.timeline:
            if item.id in ids_seen:
                issues.append(f"Duplicate clip id '{item.id}'")
            ids_seen.add(item.id)

            if item.source not in self._source_videos:
                issues.append(
                    f"Shot '{item.id}': unknown source '{item.source}'. "
                    f"Available: {list(self._source_videos.keys())}"
                )
            else:
                meta = self._metadatas.get(item.source)
                dur = meta.duration if meta else 0.0
                if dur > 0:
                    if item.source_start < 0 or item.source_end > dur:
                        issues.append(
                            f"Shot '{item.id}': range {item.source_start}-{item.source_end} "
                            f"exceeds source '{item.source}' duration {dur}s"
                        )
                if item.source_start >= item.source_end:
                    issues.append(
                        f"Shot '{item.id}': start ({item.source_start}) >= "
                        f"end ({item.source_end})"
                    )
                # Overlap detection per source
                ranges = seen_ranges.setdefault(item.source, [])
                for (s, e) in ranges:
                    if item.source_start < e and item.source_end > s:
                        issues.append(
                            f"Shot '{item.id}': overlaps existing shot on "
                            f"'{item.source}' ({s}-{e})"
                        )
                        break
                ranges.append((item.source_start, item.source_end))
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

    def _has_audio_stream(self, raw: dict | None) -> bool:
        """Return True if the ffprobe raw data contains an audio stream."""
        if not raw:
            return False
        for stream in raw.get("streams", []):
            if stream.get("codec_type") == "audio":
                return True
        return False

    def _resolve_any_video(self, name: str) -> Path | None:
        """Resolve a video path/name across output, clips, and preview dirs.

        Accepts an absolute path, a name with extension, or a bare stem.
        """
        p = Path(name)
        if p.is_absolute() and p.exists():
            return p
        candidates = [p.name, p.name if p.suffix else f"{p.name}.mp4"]
        if not p.suffix:
            candidates.append(f"{p.name}.mp4")
        # de-duplicate preserving order
        seen = set()
        names = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                names.append(c)
        for d in (self._output_dir, self._clips_dir, self._preview_dir):
            for n in names:
                full = d / n
                if full.exists():
                    return full
        # Also try the input as-is relative to output dir.
        return None

    def _resolve_clip(self, name: str) -> Path | None:
        """Resolve a clip/audio name to its file path.

        Resolution order:
          1. intermediate clips (in-memory map from this run)
          2. the clips directory (video + audio extensions)
          3. the working folder root (audio files only — e.g. background
             music placed in the project folder)

        This lets mix_audio reference a music file by its exact filename
        (e.g. 'background.mp3') without it having to be an intermediate clip.
        """
        if name in self._intermediate_clips:
            p = Path(self._intermediate_clips[name])
            if p.exists():
                return p

        from .state import AUDIO_EXTENSIONS

        # Clips dir: try the bare name with any video/audio extension.
        for ext in (".mp4", ".mov", ".mkv", ".avi", ".webm") + tuple(AUDIO_EXTENSIONS):
            p = self._clips_dir / f"{name}{ext}"
            if p.exists():
                return p

        # Working folder root: audio files only (background music).
        for ext in tuple(AUDIO_EXTENSIONS):
            p = self._working_folder / f"{name}{ext}"
            if p.exists():
                return p

        # Last resort: the name may already include its extension.
        p = self._working_folder / name
        if p.exists() and p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
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
    registry: "ToolRegistry",
    progress_cb: Callable[[str], None] | None = None,
    log_cb: Callable[[str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[str, list[dict]]:
    """Run the two-phase, multi-turn tool-calling agent loop.

    Phase 1 — Plan: only probe_video / inspect_clip / commit_edit_plan /
    update_edit_plan are available. Runs until commit_edit_plan is called
    (detected via ``registry.current_plan`` becoming non-None) or the round
    budget is exhausted.

    Phase 2 — Execute: the full tool set is available. The agent executes
    the committed plan, calling update_edit_plan if boundaries change.

    Returns (final_text, tool_log) where tool_log is a list of dicts with
    tool name, args, result, success, and duration.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tool_log: list[dict] = []
    final_text = ""

    # Callables used for dispatch (matched by __name__) and phase gating.
    phase1_callables = registry.get_plan_phase_tools()
    all_callables = registry.get_tools()

    # Schemas passed to client.chat. The plan tools are explicit Tool dicts
    # because the SDK flattens nested Pydantic params to `type: string`,
    # which would give the model no schema for the plan structure.
    phase1_schemas = registry.get_plan_phase_chat_tools()
    all_schemas = registry.get_chat_tools()

    def _active_schemas() -> list:
        return phase1_schemas if registry.current_plan is None else all_schemas

    def _phase_label() -> str:
        return "Phase 1 (plan)" if registry.current_plan is None else "Phase 2 (execute)"

    plan_committed_round: int | None = None

    for round_trip in range(MAX_AGENT_ROUND_TRIPS):
        if is_cancelled and is_cancelled():
            return "Cancelled", tool_log

        tools = _active_schemas()
        if progress_cb:
            progress_cb(
                f"{_phase_label()} — round {round_trip + 1}/{MAX_AGENT_ROUND_TRIPS}..."
            )

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

            # Enforce phase gating: reject execute-phase tools during planning.
            if registry.current_plan is None and tool_name not in {
                getattr(t, "__name__", "") for t in phase1_callables
            }:
                msg = (f"Tool '{tool_name}' is not available during the "
                       f"planning phase. Call commit_edit_plan() first.")
                result_data = {"error": msg}
                if log_cb:
                    log_cb(f"[tool] {tool_name} REJECTED (planning phase): {msg}")
            else:
                if log_cb:
                    log_cb(f"[tool] {tool_name}({args_str})")

                round_start = time.time()
                result_data = _execute_tool_call(all_callables, tool_name, args)
                round_dur = time.time() - round_start

                tool_log.append({
                    "tool": tool_name,
                    "args": args,
                    "result": result_data.get("data", result_data),
                    "success": not bool(result_data.get("error")),
                    "duration_s": round_dur,
                    "timestamp": _now_iso(),
                })

                # Detect transition into Phase 2.
                if tool_name in ("commit_edit_plan", "update_edit_plan") \
                        and registry.current_plan is not None \
                        and plan_committed_round is None:
                    plan_committed_round = round_trip
                    if log_cb:
                        log_cb(
                            f"Edit plan committed: "
                            f"{len(registry.current_plan.timeline)} shots. "
                            f"Switching to execute phase."
                        )

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
    """Execute a tool call by dispatching to the matching function.

    Returns the tool's result data as a plain dict (NOT a wrapped tool
    message). Tool methods return ``ToolResult.to_tool_message()`` which
    produces ``{"role": "tool", "content": <json string>}``; we unwrap that
    back to the inner data dict so the agent loop can detect ``"error"``
    keys and send a single-wrapped tool message to the model.
    """
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
        # Unwrap a to_tool_message() dict back to the inner data dict.
        if isinstance(result, dict) and result.get("role") == "tool" \
                and "content" in result and "error" not in result:
            try:
                return json.loads(result["content"])
            except (json.JSONDecodeError, TypeError):
                return {"error": f"Tool returned unreadable result: {result.get('content', '')[:200]}"}
        if isinstance(result, dict):
            return result
        return {"result": str(result)}
    except Exception as e:
        return {"error": f"Tool execution error: {e}"}


# --- Prompt building ---------------------------------------------------------

def build_generation_prompt(storyboard_md: str, context_md: str) -> str:
    """Build the user-message content for an initial edit generation.

    Instructs the agent to follow the two-phase workflow: first probe
    referenced sources + inspect uncertain boundaries + commit_edit_plan,
    then execute the plan.
    """
    return (
        f"## Storyboard\n\n{storyboard_md.strip()}\n\n"
        f"## Available Context\n\n{context_md.strip()}\n\n"
        f"## Task\n\n"
        f"Produce a final edited video based on the storyboard using the "
        f"two-phase workflow.\n\n"
        f"Phase 1 — Plan: call probe_video() for every source video "
        f"referenced in the storyboard. Call inspect_clip() where a "
        f"storyboard moment's exact boundaries are uncertain. Then call "
        f"commit_edit_plan() with a complete structured Edit Plan. Each "
        f"timeline item's id must match the output_name you will use in "
        f"extract_clip().\n\n"
        f"Phase 2 — Execute: with the full tool set, create each clip, "
        f"validate it with validate_clip(), apply transitions/effects, "
        f"assemble the timeline, render, and validate the output.\n\n"
        f"Use the tools provided — do not write FFmpeg commands. Reference "
        f"source videos by exact filename. Do not invent footage. Report "
        f"any gaps or issues you encounter."
    )


def build_refinement_prompt(feedback: str, edit_plan_json: str,
                             storyboard_md: str, context_md: str) -> str:
    """Build the user-message content for a refinement based on user feedback.

    The existing edit plan is supplied so the agent amends it (via
    commit_edit_plan / update_edit_plan) rather than starting blank, and
    reuses unaffected intermediate clips.
    """
    return (
        f"{REFINEMENT_INSTRUCTIONS.strip()}\n\n"
        f"## User Feedback\n\n{feedback.strip()}\n\n"
        f"## Current Edit Plan\n\n```json\n{edit_plan_json}\n```\n\n"
        f"## Storyboard\n\n{storyboard_md.strip()}\n\n"
        f"## Available Context\n\n{context_md.strip()}\n\n"
        f"## Task\n\n"
        f"Apply the user's feedback following the two-phase workflow. First, "
        f"amend the current edit plan with commit_edit_plan() (or "
        f"update_edit_plan()) to reflect only the requested changes. Then "
        f"execute only the affected shots — reuse existing intermediate "
        f"clips that are not affected (a clip already on disk with the same "
        f"name can be reused without re-extracting). Re-assemble, re-render, "
        f"and re-validate. Preserve good decisions from the current edit."
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


def clear_production(working_folder: str) -> None:
    """Delete all video production artefacts.

    Removes the entire <working_folder>/.llama-cut/video/ directory.
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

    Fallback only: the primary path is the proactive plan committed via
    commit_edit_plan / update_edit_plan during the agent run. This builder
    reconstructs a plan from extract_clip / create_transition calls when no
    committed plan exists, populating the traceability fields best-effort
    from the available tool arguments and results.
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
            clip_name = result.get("output_name", f"shot_{shot_num:02d}")
            timeline.append(TimelineItem(
                id=clip_name,
                source=args.get("video_path", ""),
                source_start=args.get("start_time", 0.0),
                source_end=args.get("end_time", 0.0),
                speed=1.0,
                intermediate_clip=clip_name,
                description=result.get("linked_storyboard_scene", ""),
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
        status="executing" if timeline else "draft",
        output_path="",
    )
