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

The app is a 9-stage guided pipeline. Stages run in order; each persists its
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

### Stage 4 — Frame Extraction (`src/pages/frame_generation_page.py`,
`src/ffmpeg/extract.py`, `src/workers/extract_worker.py`)
- Extracts representative still frames at intervals from each video.
- Output: frame image files under the working folder; tracked in
  `PipelineState.frames`.

### Stage 5 — Frame Analysis (`src/pages/select_frames_page.py`,
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
`src/video_production.py`, `src/edit_plan_executor.py`,
`src/workers/chat_agent_worker.py`, `src/workers/edit_executor_worker.py`,
`src/pages/chat_widget.py`, `src/pages/edit_plan_widget.py`,
`src/pages/debug_plan_dialog.py`)
- **Chat-driven edit plan builder.** The user converses with an LLM agent
  (`OLLAMA_WORKFLOW_MODEL`) via a chat UI (`ChatWidget`) to build an edit
  plan from the storyboard. The agent has 4 planning-only tools
  (`probe_video`, `inspect_clip`, `commit_edit_plan`, `update_edit_plan`)
  and never runs ffmpeg. The storyboard is injected as context (not shown
  on this screen); `assets/ffmpeg-skill.md` is also injected so the agent
  can construct correct command arguments.
- **Chat UX:** deep charcoal background, user messages as right-aligned
  pills, assistant messages left-aligned with bold sender label, a
  pulsing-dots "Thinking…" animation while waiting for the LLM, and
  expandable tool-call chips showing args/result/duration.
- **Edit plan = beats + commands.** The `EditPlan` model has a `timeline`
  (beats for visual display) and a `commands` list (typed ffmpeg ops for
  execution), linked by `beat_id`. Each command type (`extract_clip`,
  `create_transition`, `create_edit`, `assemble_timeline`, `mix_audio`,
  `render_video`, `validate`) has a builder that renders the actual ffmpeg
  command line.
- **LEP (Linear Edit Plan) visual.** The plan is displayed as a
  horizontal beat strip (`EditPlanWidget`) below the chat: each beat is a
  vertical stack (annotation → 16:9 thumbnail → transition pill), with
  status-colored borders. Thumbnails are extracted on-the-fly at each
  beat's midpoint and cached. Amendments (reorder, insert, delete,
  transition change) go through the chat → `update_edit_plan` → re-render.
- **Debug modal.** A "Debug" button opens a modal (`DebugPlanDialog`)
  showing the raw edit plan JSON and the queued ffmpeg commands as
  copyable command-line strings.
- **Execution.** The user clicks "Run Edit Plan" → confirmation dialog →
  `EditExecutorWorker` runs the commands sequentially via
  `EditPlanExecutor` with duration-weighted progress (Extract 20%,
  Transitions 10%, Assemble 15%, Audio 5%, Render 40%, Validate 10%) and
  safe abort (kill current subprocess). Completed clips are checkpoints
  and are skipped on re-run.
- **Failure → LLM feedback loop.** On a command failure, execution halts,
  the failed command + ffmpeg stderr are injected into the chat as a
  tool-role message, and the agent proposes a fix via `update_edit_plan`.
  The user reviews the fix and re-runs (checkpoints are reused).
- On a successful render, auto-navigates to Stage 9 (Result).
- Output: `video/chat.json` (chat transcript), `video/edit_plan.json`,
  `video/tool_log.json`, `video/thumbs/*.jpg` (beat thumbnails),
  `video/clips/*.mp4` (intermediate clips, kept as checkpoints),
  and the rendered video in `video/output/`.

### Stage 9 — Result (`src/pages/result_page.py`)
- Displays the final rendered video in a player with play/pause, seek,
  and volume controls, alongside the agent's markdown summary report
  (from `edit_plan.notes`, rendered as HTML — not raw JSON).
- Auto-navigated to from Stage 8 only when a video file exists in
  `.llama-cut/video/output/`. Also reachable via the sidebar.
- Video discovery: `find_rendered_video()` in `src/video_production.py`
  scans `video/output/` for the newest `.mp4` (by mtime). The worker also
  populates `edit_plan.output_path` post-render for a direct reference.
- Output: read-only view of the rendered video + report; no files written.

## Stage 8 — Final Video Production (V2 philosophy)

The editor is a **chat-driven, human-in-the-loop** LLM pipeline with the
goal: **accurate → deterministic → inspectable → recoverable**. The agent
is a *careful planner*, not an autonomous executor. The human reviews the
plan before execution.

Priority order (enforced in `EDITING_SYSTEM_PROMPT`):
1. Correctly interpret the storyboard
2. Select the correct source footage
3. Respect exact timestamps
4. Produce the intended sequence
5. Apply transitions/effects correctly
6. Construct correct ffmpeg commands
7. Only then optimise

### How it works
- **Chat phase:** the agent converses with the user via `ChatWidget` to
  build the edit plan. It has 4 planning-only tools: `probe_video`,
  `inspect_clip`, `commit_edit_plan`, `update_edit_plan`. It never runs
  ffmpeg. The storyboard + context + `assets/ffmpeg-skill.md` are injected
  as the first hidden user message.
- **Review phase:** the plan is displayed as a LEP horizontal beat strip
  (`EditPlanWidget`) below the chat. The user amends via chat or approves.
- **Execution phase:** `EditExecutorWorker` runs the queued commands
  sequentially via `EditPlanExecutor` with duration-weighted progress and
  safe abort. Completed clips are checkpoints (skipped on re-run).
- **Failure → feedback loop:** on a command failure, execution halts, the
  failed command + ffmpeg stderr are injected into the chat, and the agent
  proposes a fix via `update_edit_plan`. The user re-runs with checkpoint
  reuse.

### Storyboard ↔ editor coupling
- `src/editor_capabilities.py` is the single source of truth describing the
  editor's practical capabilities (plain English, no tool names). It is injected
  into the storyboard builder's prompts so the storyboard stays practical — but
  the storyboard itself must always be plain English and never reference tools.

### Deferred optimisations (do NOT implement in V2)
Proxy / draft-resolution workflows, aggressive intermediate caching,
single-pass filter graphs, render optimisation beyond preset selection, GPU /
NVENC tuning beyond the automatic fallback, parallel rendering, and
intermediate-file elimination. Once several edits have been produced
end-to-end correctly, the expensive parts can be optimised without changing the
underlying edit decisions.