# Project Guidelines

## Overview

`llama-cut` is a Python desktop GUI application built with **PyQt6**. It implements a
video frame extraction and metadata generation pipeline. All Python work runs inside a
local virtual environment (`.venv`).

## Build and Test

Install the full dependency set from `requirements.txt`:

```
python -m pip install -r requirements.txt
```

This project is not a library — install with `pip`, not a package manager.

## Dependencies

- `requirements.txt` is the single source of truth for dependencies.
- **Always** invoke pip as `python -m pip` (venv-safe), never bare `pip`.
- **Always** run `python -m pip freeze > requirements.txt` after installing or upgrading
  any package, so the lock file stays in sync with the venv.
- Never hand-edit `requirements.txt`; regenerate it with `python -m pip freeze`.

## UI (PyQt6)

- All UI code **must** use **PyQt6**. Do not use Tkinter, PySide, or other GUI toolkits.
- When working on a UI design request, load and follow the **`pyqt6-ui-designer`** skill.
- Follow the PyQt6 development rules from the **`pyqt6-ui-development-rules`** skill
  (signal/slot architecture, QSS theming, QThread concurrency, layout management,
  cross-platform rendering, MVC separation).

## Icons (Material Symbols)

- Use the **`material_icon(name, size, color=None)`** helper from `src/icons.py` for all
  Material Symbols icons. It renders the icon by **name** (ligature-based lookup), e.g.
  `material_icon("video_library", 56, COLOR_PRIMARY)`.
- **Never** use raw `\ue...` codepoints in `QLabel` text — the Material Symbols Outlined
  font does not reliably map the old Material Icons codepoint lists, which can render
  the wrong glyph (e.g. a telephone instead of a folder).
- The helper sets font-family and font-size via an **inline stylesheet** — do not
  override them with `setFont()` or a later `setStyleSheet()` call. The app-level QSS
  `*` rule (`font-family: Inter`) overrides `QFont` set via `setFont()`, so only inline
  styles (which beat app QSS) keep the icon font active.
- Pass color through the helper's `color` param; use `setAlignment()` for alignment.
- The font file lives at `assets/fonts/MaterialSymbolsOutlined.ttf` and is registered
  at startup by `register_fonts()` in `src/theme.py` (called from `main.py`). If you
  add new font files, put them in `assets/fonts/` — they are auto-registered.

## Skill Usage

- Use **`pyqt6-ui-designer`** for any UI styling / design work.
- Use **`pyqt6-ui-development-rules`** as the authoritative rule set for PyQt6 code.
- Use **`python-ffmpeg`** skill for information on using ffmpeg with python.

## Pipeline Stages (Quick Reference)

The app is an 8-stage guided pipeline. Stages run in order; each persists its
output to the working folder so work survives navigation and app restarts.
The navigation sidebar (`src/app.py`) drives stage switching via
`PipelineState` (`src/state.py`).

> **NOTE:** Keep this section up to date when stages are added, renamed, or
> their behaviour/outputs change. It is a quick reference, not a full spec.

### Stage 1 — Select Videos (`src/pages/select_videos_page.py`)
- Lists video files in the working folder; generates thumbnails
  (`src/workers/thumbnail_worker.py`); lets the user pick which to edit.
- Output: `PipelineState.selected_videos` (with probed metadata via
  `src/ffmpeg/probe.py`).

### Stage 2 — Context (`src/pages/context_page.py`, `src/context.py`)
- Author/edit Markdown context: one **Project Context** (applies to all
  videos) and per-video **Video Context**.
- Transcription and Frame Analysis slots exist here but are
  programmatic-only and disabled until generated in later stages.
- Output: `context/*.md` + `context/manifest.json` (via `ContextStore`).

### Stage 3 — Transcription (`src/pages/transcription_page.py`,
`src/transcription.py`, `src/workers/transcription_worker.py`)
- Runs speech-to-text (Whisper) on each selected video's audio.
- Output: written into the per-video Transcription context slot
  (`<stem>_transcription.md`).

### Stage 4 — Frame Generation (`src/pages/frame_generation_page.py`,
`src/ffmpeg/extract.py`, `src/workers/extract_worker.py`)
- Extracts representative still frames at intervals from each video.
- Output: frame image files under the working folder; tracked in
  `PipelineState.frames`.

### Stage 5 — Analyse Frames (`src/pages/select_frames_page.py`,
`src/frame_analysis.py`, `src/workers/frame_analysis_worker.py`)
- Sends extracted frames to a vision model (`OLLAMA_VISION_MODEL`) and
  writes a description + timestamp per frame. Runs are appended under a
  `## Run — <ts>` header.
- Output: written into the per-video Frame Analysis context slot
  (`<stem>_frame_analysis.md`).

### Stage 6 — Context Review (`src/pages/context_review_page.py`,
`src/context_review.py`)
- Assembles everything gathered so far (Project Context + per-video Video
  Context, Transcription, Frame Analysis) into one scrollable document with
  inline frame images and thumbnails. Any section is editable (toggle
  Edit/Done); edits autosave back to the individual `.md` files.
- Export: `video_context_report.md` (plain Markdown with frame images).
- This is the complete, editable picture the downstream storyboard consumes.

### Stage 7 — Storyboard (`src/pages/storyboard_page.py`, `src/storyboard.py`,
`src/workers/storyboard_worker.py`)
- LLM (`OLLAMA_WORKFLOW_MODEL`) builds a scene-by-scene creative plan from
  the assembled context + a user creative brief. Supports iterative
  refinement with history. The editor's practical capabilities (plain
  English, no tool names) are injected from `src/editor_capabilities.py` so
  the storyboard stays implementable — but the storyboard itself is always
  plain English.
- Output: `storyboard/storyboard.md` + `storyboard/history.json`.

### Stage 8 — Final Video Production (`src/pages/video_production_page.py`,
`src/video_production.py`, `src/workers/video_production_worker.py`)
- Agentic, two-phase LLM editor (plan-then-execute) renders the final video
  from the storyboard. See the "Stage 8 — Final Video Production" section
  below for the full philosophy, tool set, and deferred optimisations.
- Output: `video/clips/*.mp4` (intermediate clips, kept as checkpoints),
  `video/edit_plan.json`, `video/tool_log.json`, and the rendered video in
  `video/output/` (final) or `video/preview/` (preview preset).

## Stage 8 — Final Video Production (V1 philosophy)

The editor is an **agentic, two-phase** LLM pipeline (plan-then-execute) with
the goal: **accurate → deterministic → inspectable → recoverable**. The agent
is a *careful* editor, not a fast editor.

Priority order (enforced in `EDITING_SYSTEM_PROMPT`):
1. Correctly interpret the storyboard
2. Select the correct source footage
3. Respect exact timestamps
4. Produce the intended sequence
5. Apply transitions/effects correctly
6. Verify the resulting video
7. Only then optimise rendering performance

### How it works
- **Phase 1 — Plan:** the agent calls `probe_video` / `inspect_clip`, then
  `commit_edit_plan` with a structured `EditPlan`. The plan is the source of
  truth and is persisted to `video/edit_plan.json`. It can be amended with
  `update_edit_plan`.
- **Phase 2 — Execute:** the full tool set is available. Clip ids in the plan
  become `output_name`s in `extract_clip` calls, so every clip is traceable to
  its plan entry. Each clip is validated with `validate_clip`.

### Storyboard ↔ editor coupling
- `src/editor_capabilities.py` is the single source of truth describing the
  editor's practical capabilities (plain English, no tool names). It is injected
  into the storyboard builder's prompts so the storyboard stays practical — but
  the storyboard itself must always be plain English and never reference tools.

### Deferred optimisations (do NOT implement in V1)
Proxy / draft-resolution workflows, aggressive intermediate caching,
single-pass filter graphs, render optimisation beyond preset selection, GPU /
NVENC tuning beyond the automatic fallback, parallel rendering, and
intermediate-file elimination. Once several edits have been produced
end-to-end correctly, the expensive parts can be optimised without changing the
underlying edit decisions.