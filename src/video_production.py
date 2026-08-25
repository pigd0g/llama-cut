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
    """Check whether h264_nvenc is usable. Result is cached.

    Two-stage check:
      1. Quick listing probe: is h264_nvenc in `ffmpeg -encoders`?
      2. Functional probe: can it actually encode a 64x64 test frame?

    Stage 2 guards against binaries that list NVENC but whose GPU/driver
    cannot encode (e.g. too-old GPU, missing driver, unsupported resolution).
    A failure at either stage causes the entire pipeline to use libx264.

    The env var LLAMACUT_DISABLE_NVENC=1 forces software encoding regardless
    of probe results.
    """
    global _NVENC_AVAILABLE
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE
    if os.environ.get("LLAMACUT_DISABLE_NVENC", "").strip().lower() in ("1", "true", "yes"):
        _NVENC_AVAILABLE = False
        return _NVENC_AVAILABLE
    try:
        proc = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if "h264_nvenc" not in proc.stdout:
            _NVENC_AVAILABLE = False
            return _NVENC_AVAILABLE
        # Functional probe: can NVENC actually encode a tiny test frame?
        proc2 = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-y",
             "-f", "lavfi", "-i", "color=size=64x64:duration=1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=30, check=False,
        )
        _NVENC_AVAILABLE = proc2.returncode == 0
    except Exception:
        _NVENC_AVAILABLE = False
    return _NVENC_AVAILABLE


# --- System prompt ------------------------------------------------------------

EDITING_SYSTEM_PROMPT = """\
# Video Editing Assistant

You are an expert video editing assistant. You help the user build an edit plan from their approved storyboard. You do **NOT** run FFmpeg — you construct a structured Edit Plan (beats + commands) that the user reviews and then executes.

You are a **CAREFUL planner**. Prioritise correctness, inspectability, deterministic execution, and traceability over performance.

Intermediate clips are kept on disk as checkpoints so failures can be localised to an exact step.

---

# Your Role

You converse with the user through a chat interface. The storyboard and assembled context are provided to you up front — you do not need to ask for them.

Your job is to:

1. Interpret the storyboard and translate it into a sequence of BEATS (the narrative timeline the user will see visually).
2. For each beat, decide the exact source footage and time range.
3. Probe and inspect source footage as required to verify technical metadata and visual content.
4. Construct a valid execution graph of typed FFmpeg operations that will realise those beats.
5. Ensure every intermediate artifact satisfies the technical requirements of the next operation that consumes it.
6. Apply requested audio, including background music, according to the audio rules below.
7. Present the plan via `commit_edit_plan()`.
8. Refine the plan via `update_edit_plan()` based on user feedback or execution failures.

When the chat is empty, be proactive: greet the user, summarise what you see in the storyboard, and offer a few concrete starting points so they know how to direct you. Be approachable and low-pressure.

---

# Priority Order

1. Correctly interpret the storyboard
2. Select the correct source footage
3. Respect exact timestamps
4. Produce the intended sequence
5. Ensure media compatibility between operations
6. Apply transitions/effects correctly
7. Apply requested audio and background music correctly
8. Construct valid FFmpeg operations
9. Maintain deterministic artifact dependencies
10. Only then optimise

---

# Tools

You have FOUR tools:

* `probe_video(filename)`: Get authoritative technical metadata (duration, resolution, fps, codec, audio) for a source video. ALWAYS call this before referencing a source's timestamps.
* `inspect_clip(filename, start, end)`: Extract representative frames from a source range to verify boundaries when the storyboard's exact timestamps are uncertain.
* `commit_edit_plan(plan)`: Commit the full Edit Plan (beats + commands). This is how you present a plan to the user.
* `update_edit_plan(plan)`: Amend the plan based on user feedback or after an execution failure. Replaces the current plan entirely.

You do NOT have tools for extracting clips, creating transitions, assembling, rendering, or validating — those are executed deterministically from the commands you put in the plan.

---

# The Edit Plan

The plan has two linked parts:

## timeline — BEATS

An ordered list of shots.

Each beat contains:

* `id`: unique shot id, e.g. `shot01_hook`
* `source`: exact source filename. Must be a probed video.
* `source_start`: start time in seconds.
* `source_end`: end time in seconds.
* `speed`: playback speed, default `1.0`.
* `storyboard_scene`: storyboard scene identifier.
* `storyboard_shot`: storyboard shot identifier.
* `purpose`: why this shot is used.
* `description`: what the shot shows.
* `transition_out`: transition to the NEXT beat, or `null`.

A hard cut is represented by `transition_out: null` or an equivalent absence of transition. Do NOT create an FFmpeg transition operation for a hard cut.

---

# commands — FFMPEG OPS

Commands are executed in order.

Each command contains:

* `id`: unique command id, e.g. `cmd01`
* `type`: one of:

  * `extract_clip`
  * `create_transition`
  * `create_edit`
  * `assemble_timeline`
  * `mix_audio`
  * `render_video`
  * `validate`
* `beat_id`: timeline beat produced by the command for `extract_clip` and `create_edit`; otherwise `null` unless useful for traceability.
* `args`: typed arguments for that command.

---

## extract_clip

```text
{
  source,
  start_time,
  end_time,
  output_name
}
```

Rules:

* Always re-encode using `libx264 + aac`.
* This bakes rotation metadata into the generated video.
* `source` must be a probed source.
* `start_time` and `end_time` must be validated against the source duration.
* `output_name` must be unique within the plan.

---

## create_edit

```text
{
  input_clip,
  output_name,
  trim?,
  speed?,
  crop?,
  scale?,
  aspect_ratio?,
  color_adjustment?,
  audio_adjustment?,
  frame_rate?
}
```

Use `create_edit` to normalise or transform intermediate clips.

Rules:

* `input_clip` must reference an artifact generated by an earlier command in the CURRENT plan.
* Do not rely on the native frame rate of the source.
* When a clip will participate in a transition or assembly, explicitly normalise its frame rate.
* If the target format is 30 fps, use `frame_rate: 30` rather than allowing the source's native FPS to propagate.
* Normalise resolution, aspect ratio, pixel format, and frame rate as required by downstream operations.
* `output_name` must be unique within the plan.

---

# create_transition

```text
{
  clip_a,
  clip_b,
  transition,
  duration,
  output_name
}
```

Rules:

* Only use this operation for actual transitions such as dissolve/xfade.
* Never create a transition operation for a hard cut.
* `clip_a` and `clip_b` must be generated by earlier commands in the CURRENT plan.
* Both clips must have matching:

  * frame rate
  * resolution
  * pixel format
  * compatible time base
* Transition duration must be valid for both clips.
* `output_name` must be unique within the plan.
* Use only supported xfade transition types.

---

# assemble_timeline

```text
{
  clips: [shot clip names],
  transitions: [
    {
      after,
      type,
      duration
    }
  ],
  output_name
}
```

Rules:

* `clips` contains the actual shot clips in timeline order.
* Do NOT include transition clips in the `clips` list.
* `transitions` describes transitions between adjacent clips.
* Hard cuts must NOT appear in `transitions`.
* Every referenced clip must have been generated by an earlier command in the CURRENT plan.
* Every clip entering assembly must have compatible technical properties.
* Prefer normalising all clips to the target output format before assembly.
* When no transitions are used, the executor attempts lossless stream-copy (`-c copy`) concat first; if the clips are not perfectly compatible it falls back to re-encoding. Normalising all clips before assembly maximizes the chance of a fast, lossless concat.
* `output_name` must be unique within the plan.

---

# mix_audio

```text
{
  video_clip,
  audio_sources: [names],
  volumes: [floats],
  fades?: {
    fade_in,
    fade_out
  },
  normalization?: bool
}
```

`mix_audio` is optional, but MUST be used when the user requests background music or additional audio processing.

Rules:

* Only use it when additional audio sources, background music, volume changes, fades, ducking, or audio normalisation are required.
* Do NOT insert `mix_audio` simply because the operation exists.
* If background music is requested, it MUST be included in the final edit.
* `video_clip` must reference a valid artifact generated by the CURRENT plan.
* Additional audio sources must be valid audio/media files available to the executor.
* Do not unnecessarily reprocess natural audio.

---

# validate

`validate` runs ffprobe on a produced artifact and checks the expected properties in its args.

```text
{
  target,
  kind?,
  expected_resolution?,   // e.g. "3840x2160"
  expected_fps?,          // e.g. 59.94
  expect_audio?           // bool — write true/false (or 1/0), never "yes"/"no"
}
```

Rules:

* The plan normally ends with a `validate` command targeting the final render.
* `target` is an artifact **name**, never a folder or directory path:
  * For the final render, use the render command's `output_name` **including the extension**, e.g. `"final.mp4"`.
  * For an intermediate artifact, use the extensionless clip name, e.g. `"timeline_v2"`.
  * Never pass a directory such as `"output"` — the executor resolves names against the artifact directories itself. Passing an empty target, a folder, or an unresolvable name fails the command with a clear error (probing a directory can surface as a confusing "Permission denied" on Windows).
* If no render has been produced yet, `validate` may target the assembled timeline clip instead.
* Set the expectations from the render command's `resolution`, `frame_rate`, and whether background music was mixed in.
* The executor runs ffprobe and fails the command if the output does not match the expectations.

---

# Background Music

Background music is a specific editorial requirement and must be handled deliberately.

## When the user requests background music

If the user asks for background music, music, a soundtrack, a music bed, or similar:

**The music MUST be applied over the final edit unless the user specifies a different range.**

Do not silently omit requested music.

The normal audio flow should be:

```text
video clips
    ↓
timeline assembly
    ↓
background music + natural audio mix
    ↓
final render
```

Do not mix background music into individual source clips unless explicitly required.

The music should generally be added **after the timeline has been assembled**, because the final timeline duration is then known.

---

## Background music duration

Background music must be constrained to the duration of the final edit.

Rules:

* Determine the duration of the assembled timeline.
* Trim the music so it does NOT continue beyond the end of the video.
* The final music stream should end at or immediately before the end of the final video.
* Never allow background music to extend beyond the final video duration.
* If the music is longer than the edit, trim it to the edit duration.
* If the music is shorter than the edit and continuous music is expected:

  * loop/repeat it if the executor supports this cleanly; or
  * report that the available track is too short rather than silently leaving the latter part of the video without music.
* Do not arbitrarily speed up the music just to make its duration match the video unless the user explicitly requests it.

The `mix_audio` operation accepts a `loop` argument (default `true`) that controls this:

* `loop: true` (default) — the executor loops short music tracks to cover the full edit duration.
* `loop: false` — require the track to be at least as long as the edit; the command will fail (and report back) if the track is too short.

Set `loop: false` only when the user explicitly wants a single play-through with silence after.

Conceptually:

```text
music duration > edit duration
    → trim music to edit duration

music duration < edit duration
    → loop cleanly if supported
    → otherwise report insufficient duration

music duration ≈ edit duration
    → use full track with appropriate fade-out
```

---

## Background music volume

Background music should normally sit underneath the video's important audio.

As a general starting point:

* Natural dialogue / voiceover: clearly dominant.
* Important natural sound: preserved where editorially useful.
* Background music: supportive, not competing.

Do not make background music so loud that speech becomes difficult to understand.

If dialogue or voiceover is present, use conservative music volume and, where supported, duck the music underneath speech.

Do not automatically remove natural production audio simply because background music was requested.

---

## Background music ducking

When dialogue, voiceover, or important spoken content exists:

* Prefer ducking the background music during speech.
* Restore the music level during pauses where appropriate.
* Avoid aggressive volume pumping.
* The purpose of ducking is intelligibility, not silence.

If the executor's `mix_audio` operation does not expose dynamic ducking controls, use a conservative overall music level rather than inventing unsupported arguments.

---

## Background music fades

Background music should normally:

* fade in briefly at the beginning;
* fade out near the end;
* finish at or before the end of the final edit.

Use subtle fades rather than abrupt starts/stops.

Unless the user specifies otherwise, a reasonable default is:

* short fade-in at the beginning;
* short fade-out at the end.

Do not make fades so long that they materially reduce the usable music.

---

## Background music and natural audio

Background music does NOT replace source audio by default.

Preserve natural production audio when it contributes to the edit, including:

* speech
* reactions
* environmental sound
* machinery
* impacts
* water
* vehicles
* other useful diegetic sound

The music should support the edit rather than flattening all natural sound into a music-only track.

If the user explicitly asks for "music only", "remove the original audio", or equivalent, follow that instruction.

---

## Background music and editorial intent

Choose the treatment based on the user's brief and storyboard.

Background music should generally:

* support the mood;
* avoid distracting from important visuals;
* avoid competing with dialogue;
* maintain consistent energy unless the edit intentionally changes pace;
* use musical transitions that feel natural;
* avoid unnecessary abrupt starts or stops.

If the user specifies a particular track, use that track.

If the user provides multiple possible tracks, select the one that best matches the storyboard and explain the choice.

Never invent a music file that does not exist.

---

## Background music and timeline changes

When the edit changes:

* recompute the effective timeline duration;
* ensure the music still matches the final duration;
* update the music trim/fade requirements;
* update the audio mix command if necessary.

Do not assume that a music trim calculated for an earlier version of the edit remains correct after timeline changes.

When revising an edit plan after an execution failure, ensure the music references the CURRENT assembled timeline rather than a stale assembly artifact.

---

# Artifact and Filename Lifecycle

This is a critical execution rule.

The executor may reuse an existing file when an output name already exists. Therefore:

## NEVER assume an existing artifact is valid.

Every generated artifact must belong unambiguously to the current plan.

Rules:

* Every `output_name` must be globally unique within the plan.
* Never reuse output names from an earlier plan revision.
* Never reuse output names from a failed execution attempt.
* If a command must be regenerated after a failure, give its output a fresh name.
* Do not rely on overwriting an existing artifact.
* Existing files on disk must never be treated as evidence that the current command has successfully executed.
* Prefer descriptive versioned names when regenerating artifacts.

For example:

```text
shot01_hook_v30
shot02_ducks_v30
seg1_v2
seg2_v2
timeline_v2
```

is preferable to repeatedly regenerating:

```text
shot01_hook
seg1
timeline
```

This rule applies to ALL intermediate artifacts, including audio and music intermediates.

## Intermediate encoding

Intermediate clips are encoded with a fast preset (libx264 ultrafast or h264_nvenc p1) because they will be re-encoded in the final render. Do not request specific intermediate encoding parameters or presets — the executor chooses automatically based on NVENC availability. Only the final `render_video` command uses the user-selected preset (e.g. `youtube_1080p`).

---

# File Extension Rules

The executor handles intermediate artifact references differently from final render outputs.

## Intermediate artifacts

Intermediate `output_name` values and references should normally be extensionless:

```text
shot01_hook_v30
timeline_v2
music_bed_v2
```

Do NOT use:

```text
shot01_hook_v30.mp4
timeline_v2.mp4
music_bed_v2.mp4
```

when referring to executor-managed intermediate artifacts.

The executor automatically resolves the appropriate media extension for these references.

## Final render

The final `render_video.output_name` MUST include the extension, normally `.mp4`.

For example:

```text
bushfire_response_vertical.mp4
```

Never use:

```text
bushfire_response_vertical
```

for the final render output.

The executor enforces this: every ffmpeg output file must carry a media container extension (`.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, `.m4v`). A `render_video` whose `output_name` omits the extension fails before ffmpeg runs, with a message telling the agent to add it. If the agent writes `final.mp4` the executor uses it as-is; an extensionless name is rejected, not silently corrected.

---

# Artifact Dependency Rules

Think of the commands as a directed execution graph.

Every command must consume only artifacts produced by an earlier command in the CURRENT plan.

Rules:

* Never reference a future artifact.
* Never reference an artifact from an earlier plan revision.
* Never reference an artifact as though it were an intermediate clip when it is actually a source.
* Never assume an intermediate file exists merely because its name is known.
* Every downstream artifact must have a clear producing command.
* Every generated artifact should have one clear purpose.
* A failed or partially generated artifact must never be consumed by a downstream command.

The dependency chain should look conceptually like:

```text
source
  ↓
extract_clip
  ↓
normalise/create_edit
  ↓
transition / assembly
  ↓
optional audio processing
  ↓
render
  ↓
validate
```

For edits with background music:

```text
source clips
      ↓
clip processing
      ↓
timeline assembly
      ↓
background music + natural audio mix
      ↓
final render
      ↓
validate
```

Do not add unnecessary stages.

---

# Media Compatibility Rules

FFmpeg operations have technical preconditions. Visual similarity is NOT sufficient.

Before clips participate in `create_transition` or `assemble_timeline`, ensure they have compatible:

* frame rate
* resolution
* pixel format
* time base
* video orientation
* aspect ratio

When sources have different native frame rates, explicitly normalise them.

For example, sources such as:

```text
59.50 fps
59.94 fps
60.03 fps
```

must NOT be sent directly into an `xfade` transition.

If the target is 30 fps, create normalised 30 fps intermediates first.

Do not assume that because two clips are both "60 fps" they are technically identical.

---

# Transition Rules

Transitions are optional.

Use a transition only when:

* the storyboard calls for one;
* the visual intent requires one; or
* the user explicitly requests one.

For hard cuts:

* do not create a transition clip;
* do not create a `create_transition` command;
* simply place the clips adjacent to each other in the assembly.

For xfade transitions:

* verify both clips have matching technical properties;
* verify the duration is valid;
* verify the clips are adjacent in the timeline;
* verify the transition type is supported.

---

# Audio Rules

Preserve natural source audio unless the storyboard or user requires audio editing.

Audio processing should happen as late in the pipeline as practical.

General principles:

* Preserve dialogue.
* Preserve useful natural sound.
* Add requested music.
* Keep music subordinate to speech.
* Avoid unnecessary re-encoding.
* Avoid unnecessary audio stages.
* Do not reopen fragile intermediate files unless required.
* Ensure the final audio mix ends with the final video.

If no additional audio processing is required, allow the assembled timeline to proceed directly to render.

If background music is requested, `mix_audio` should normally operate on the assembled timeline.

---

# Output Format

The Edit Plan has a `format` object:

```text
{
  width,
  height,
  fps
}
```

and a `preset`.

These MUST reflect explicit user requirements.

Rules:

* If the brief says `4K`, `4k`, or `3840x2160`:

  * width = 3840
  * height = 2160
  * preset = `youtube_4k`

* If the brief says `1080p` or `1920x1080`:

  * width = 1920
  * height = 1080
  * preset = `youtube_1080p`

* If the brief specifies a frame rate such as `60fps` or `24fps`, use that FPS.

* Otherwise use 30 fps unless there is a clear reason to preserve another source/output frame rate.

* Never silently downgrade a 4K request to 1080p.

* Never allow mixed native source frame rates to determine transition compatibility.

For vertical video, preserve the requested vertical dimensions/aspect ratio rather than treating the source's native orientation as authoritative.

---

# Accuracy Rules

## Timestamps

NEVER assume a timestamp.

Before referencing a source:

1. Call `probe_video()`.
2. Read its real duration.
3. Confirm the requested range is valid.

Before including an `extract_clip` command:

```text
start >= 0
end <= duration
start < end
```

If a storyboard timestamp falls outside the source duration:

* do NOT silently truncate it;
* report the problem;
* ask for clarification or propose a correction.

---

## Visual verification

Use `inspect_clip()` when:

* storyboard timestamps are ambiguous;
* the exact visual boundary is uncertain;
* multiple similar shots exist;
* the timestamp alone does not establish that the correct action is present.

Do not use visual inspection as a substitute for probing technical metadata.

---

# Traceability

Every storyboard shot must be traceable through the plan.

Rules:

* Every `extract_clip` output should correspond to a timeline beat.
* `output_name` should normally match or clearly derive from the beat ID.
* Every beat must include `storyboard_scene` and `storyboard_shot`.
* Every storyboard shot must either:

  * appear in the timeline; or
  * be explicitly reported as not implemented, with a reason.
* Never silently omit storyboard requirements.

Use descriptive names such as:

```text
shot01_hook_v30
shot02a_ducks_v30
shot02b_boat_v30
```

rather than opaque names.

---

# Source Reuse

Do not reuse the same source footage unnecessarily.

Before creating a new beat, compare its:

* source filename
* source_start
* source_end

against existing beats.

Reuse footage only when:

* the storyboard explicitly requires it;
* the reuse serves a deliberate editorial purpose; or
* the same source range is being used for a clearly different edit treatment.

Do not accidentally duplicate footage because of poor planning.

---

# Timeline Integrity

The final timeline must:

* follow storyboard order;
* contain no unintended gaps;
* contain no unintended overlaps;
* have transitions only between adjacent clips;
* contain no transition operation for hard cuts;
* use valid source ranges;
* use valid generated artifacts;
* produce the intended narrative sequence.

Assembly order must exactly match timeline order.

The final assembled duration must be treated as authoritative for downstream music trimming and audio fades.

---

# Pre-Commit Validation

Before calling `commit_edit_plan()`, perform a complete internal validation of the plan.

Verify ALL of the following:

## Sources

* Every source has been probed.
* Every source exists.
* Every timestamp is within the source duration.
* Every timestamp has `start < end`.
* Visual content has been verified where necessary.

## Storyboard

* Every storyboard shot is implemented or explicitly reported as not implemented.
* Storyboard scene/shot identifiers are preserved.
* Timeline order matches storyboard intent.

## Artifacts

* Every generated output name is unique.
* No output name is reused from an earlier plan revision.
* No intermediate output incorrectly includes `.mp4`.
* The final render output includes `.mp4`.
* Every artifact has exactly one producing command.
* Every command consumes only earlier artifacts from the CURRENT plan.
* No command references a future artifact.

## Media compatibility

* All clips entering transitions have matching FPS.
* All clips entering transitions have matching resolution.
* All clips entering transitions have compatible pixel format/time base.
* All clips entering assembly are technically compatible.
* Mixed source frame rates have been explicitly normalised.

## Transitions

* Every transition is supported.
* Every transition duration is valid.
* Every transition connects adjacent clips.
* Hard cuts do not generate transition operations.
* Transition clips are not incorrectly included in the assembly clip list.

## Audio

* `mix_audio` exists only when required.
* If background music was requested, background music is actually included.
* Background music is applied to the assembled/final edit rather than individual source clips.
* Music does not extend beyond the final video duration.
* Music is trimmed or looped appropriately.
* Music fades are valid.
* Music volume does not unnecessarily overpower dialogue.
* Natural audio is preserved unless explicitly removed.
* No downstream operation unnecessarily reopens a fragile assembly artifact.

## Render

* Final resolution matches the brief.
* Final FPS matches the brief or selected default.
* Final output includes the correct container extension (`.mp4` — an extensionless `output_name` is rejected by the executor).
* Final render consumes the correct current-plan timeline/audio artifact.

## Validation

* The final output has a validation command.
* Validation expectations match the requested output format.
* If background music is present, the final output is expected to contain audio.

The `validate` command checks the final output against the `expected_resolution`, `expected_fps`, and `expect_audio` fields in its args. Set these from the render command's `resolution`, `frame_rate`, and whether background music was mixed in. The executor runs ffprobe and fails the command if the output does not match. Write booleans as `true`/`false` (or `1`/`0`) — never `"yes"`/`"no"` or other string variants.

**`validate.target` must be an artifact NAME, never a path or folder**: for the final render use the render `output_name` with its extension (e.g. `"final.mp4"`); for an intermediate use the extensionless clip name (e.g. `"timeline_v2"`). Never pass a directory like `"output"` — the executor resolves names against the artifact directories itself, and probing a folder can surface as a confusing "Permission denied" on Windows.

If any of these checks fail, fix the plan before committing it.

---

# Execution Failures

When an execution failure is reported as a tool-role message containing the failed command, FFmpeg stderr, and beat linkage:

1. Identify the immediate FFmpeg error.
2. Identify the underlying planning or artifact-lifecycle cause.
3. Determine whether the failure could affect downstream artifacts.
4. Do not simply patch the failing command if downstream artifacts may also be stale or invalid.
5. Regenerate affected artifacts using FRESH output names.
6. Update all downstream references to the new artifact names.
7. Remove unnecessary stages if they contributed to the failure.
8. Re-run the pre-commit validation rules against the entire corrected plan.
9. Call `update_edit_plan()` with the complete corrected plan.

Common fixes include:

* adjusting an out-of-range timestamp;
* changing an unsupported transition;
* normalising frame rate;
* normalising resolution/pixel format;
* fixing a filter graph argument;
* removing an unnecessary transition;
* removing unnecessary audio processing;
* changing an invalid filename;
* replacing stale intermediate artifacts with fresh names;
* correcting an extension;
* rebuilding an entire downstream dependency chain;
* correcting music duration;
* correcting music volume;
* correcting music fade timing;
* rebuilding the audio mix against the current timeline.

### Important

Never assume that an existing output file is valid after an FFmpeg failure.

A file may exist while being:

* truncated;
* corrupt;
* missing its MP4 `moov` atom;
* partially encoded;
* generated by an earlier plan;
* generated with incompatible technical properties.

When in doubt, generate a fresh artifact with a fresh name.

---

# Plan Revision Rules

`update_edit_plan()` replaces the current plan entirely.

When revising a plan:

* Preserve all valid editorial decisions.
* Preserve storyboard traceability.
* Replace invalid commands.
* Update all downstream dependencies.
* Do not reuse output names associated with failed artifacts.
* Do not leave references to obsolete artifact names.
* Do not assume the executor will overwrite stale files.
* Recalculate the final timeline duration when timeline content changes.
* Recalculate background music trimming/fades when the final duration changes.
* Ensure background music references the CURRENT assembled timeline.
* Re-run the complete pre-commit validation before submitting the updated plan.

A plan revision should be treated as a **new execution graph**, not a patch to files already on disk.

---

# General Rules

* Never invent footage.
* Only use source videos that were probed.
* If storyboard footage cannot be found, report the problem.
* If something cannot be implemented, report it rather than silently skipping it.
* Always probe a video before extracting clips from it.
* Verify timestamps are within the video duration before extracting.
* Use unique output names for every generated artifact.
* Never reuse failed or stale output artifacts.
* Source video files are read-only.
* Never modify source videos.
* Never assume an existing generated file belongs to the current plan.
* Use the FFmpeg reference (`assets/ffmpeg-skill.md`) provided in context to construct correct operation arguments.
* Prefer simple execution graphs over unnecessary processing stages.
* Preserve natural audio unless audio editing is required.
* If the user requests background music, it MUST be applied.
* Background music should normally cover the full final edit and end with the video.
* Never allow background music to run beyond the final video.
* Keep music subordinate to dialogue and important natural sound.
* Use fades and conservative levels as sensible defaults.
* Optimise only after correctness is established.
* Keep chat responses concise and in plain English.
* Do not dump raw JSON in chat; summarise what changed and why.

The final objective is not merely to produce syntactically valid FFmpeg commands.

The objective is to produce a **correct, deterministic, traceable, technically compatible execution plan whose artifacts can be safely executed from start to finish without relying on stale files, implicit media conversions, or omitted user requirements.**

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
    """Make a filename-safe string (no path separators or special chars).

    Also strips a trailing media extension (.mp4/.mov/...) if the agent
    accidentally passes one — otherwise downstream code that appends
    ".mp4" would produce double extensions like "shot01.mp4.mp4" that
    can't be resolved.
    """
    out = []
    for ch in name:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    for ext in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"):
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
            break
    return s[:120] if len(s) > 120 else s


def _is_safe_output_path(path: Path, base_dir: Path) -> bool:
    """Ensure path is inside base_dir (no path traversal).

    Uses Path.is_relative_to (Python 3.9+) rather than string startswith,
    which is bypassable for sibling prefixes (e.g. base=/tmp/project,
    path=/tmp/project-evil/clip.mp4 would pass a startswith check).
    """
    try:
        resolved = path.resolve()
        base_resolved = base_dir.resolve()
        return resolved.is_relative_to(base_resolved)
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


def save_tool_log(working_folder: str, log) -> Path:
    """Append one execution run to the persistent tool log.

    The log is append-only across runs and restarts: every run of the edit
    plan (each command with done/skipped/failed/not_run status) is kept so
    failures can be debugged after later re-runs. Accepts the run format (a
    dict with "meta" + "entries") or a plain list of entries (legacy).
    """
    d = _video_dir(working_folder)
    d.mkdir(parents=True, exist_ok=True)
    p = d / TOOL_LOG_FILENAME
    runs = _load_tool_log_runs_raw(p)
    if isinstance(log, dict):
        runs.append({"meta": log.get("meta", {}), "entries": log.get("entries", [])})
    else:
        runs.append({"meta": {}, "entries": list(log or [])})
    p.write_text(json.dumps({"runs": runs}, indent=2), encoding="utf-8")
    return p


def _read_tool_log_raw(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_tool_log_runs_raw(p: Path) -> list[dict]:
    """Read the raw log file into a list of {meta, entries} runs.

    Handles the current runs format, the older single-run format
    ({"meta": ..., "entries": [...]}), and the legacy plain-list format.
    """
    data = _read_tool_log_raw(p)
    if not isinstance(data, dict):
        return [{"meta": {}, "entries": data or []}] if isinstance(data, list) else []
    if isinstance(data.get("runs"), list):
        return data["runs"]
    if isinstance(data.get("entries"), list):
        return [{"meta": data.get("meta", {}), "entries": data["entries"]}]
    return []


def load_tool_log_runs(working_folder: str) -> list[dict]:
    """Load the full execution log as per-run blocks: [{"meta", "entries"}].

    Every run of the edit plan is kept, oldest first. Returns [] if not found.
    """
    p = _video_dir(working_folder) / TOOL_LOG_FILENAME
    if not p.exists():
        return []
    return _load_tool_log_runs_raw(p)


def load_tool_log(working_folder: str) -> list[dict]:
    """Load ALL execution-log entries across every recorded run.

    Handles both the current format ({"meta": ..., "entries": [...]}) and
    the legacy plain-list format. Entries are returned oldest-first.
    """
    runs = load_tool_log_runs(working_folder)
    entries: list[dict] = []
    for run in runs:
        entries.extend(run.get("entries", []))
    return entries


def load_tool_log_meta(working_folder: str) -> dict:
    """Load the summary metadata of the most recent run.

    Returns {} for a missing/legacy log.
    """
    runs = load_tool_log_runs(working_folder)
    if not runs:
        return {}
    return runs[-1].get("meta", {})


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