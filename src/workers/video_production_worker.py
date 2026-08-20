"""Stage 8 worker — runs the editing agent in a background QThread.

Phases:
  1. Validate config
  2. Build Ollama client
  3. ffprobe all selected videos
  4. Assemble context markdown (reuse build_context_markdown from storyboard)
  5. Load latest storyboard
  6. Build the user prompt (storyboard + context + instructions)
  7. Build ToolRegistry with 9 tools
  8. Run the multi-turn agent loop (run_editing_agent)
  9. Build + persist edit plan and tool log
  10. Emit finished_success or finished_error
"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from ..context import ContextStore
from ..context_review import load_assembled
from ..storyboard import (
    build_context_markdown,
    load_history,
    load_latest_storyboard,
)
from ..video_metadata import VideoMetadata, extract_metadata
from ..video_production import (
    EDITING_SYSTEM_PROMPT,
    ToolRegistry,
    build_edit_plan_from_tool_log,
    build_generation_prompt,
    build_refinement_prompt,
    build_ollama_client,
    is_config_valid,
    load_edit_plan,
    load_video_production_config,
    run_editing_agent,
    save_edit_plan,
    save_tool_log,
)


class VideoProductionWorker(QThread):
    """Run the video production agent workflow in a background thread."""

    # str — human-readable status message
    progress = pyqtSignal(str)
    # str — detailed log line
    log = pyqtSignal(str)
    # str, str — tool name, args json (for live tool log display)
    tool_called = pyqtSignal(str, str)
    # dict — edit plan dict on success
    finished_success = pyqtSignal(object)
    # str — error message
    finished_error = pyqtSignal(str)

    def __init__(
        self,
        feedback: str,
        existing_edit_plan_json: str | None,
        working_folder: str,
        selected_videos: list,
        context_store: ContextStore,
        is_refinement: bool,
        render_preset: str = "preview",
        parent=None,
    ):
        super().__init__(parent)
        self._feedback = feedback
        self._existing_edit_plan_json = existing_edit_plan_json
        self._working_folder = working_folder
        self._selected_videos = list(selected_videos)
        self._context_store = context_store
        self._is_refinement = is_refinement
        self._render_preset = render_preset
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            self.log.emit(f"Unexpected error: {e}")
            self.finished_error.emit(str(e))

    def _run(self) -> None:
        # --- Validate config ---
        config = load_video_production_config()
        ok, msg = is_config_valid(config)
        if not ok:
            self.log.emit(msg)
            self.finished_error.emit(msg)
            return

        self.progress.emit("Connecting to Ollama...")
        self.log.emit(
            f"Connecting to Ollama at {config.host} "
            f"(model: {config.model})..."
        )
        try:
            client = build_ollama_client(config)
        except Exception as e:
            self.log.emit(f"Failed to build Ollama client: {e}")
            self.finished_error.emit(f"Failed to build Ollama client: {e}")
            return

        if self._cancel:
            self.finished_error.emit("Cancelled")
            return

        # --- Phase 1: ffprobe all selected videos ---
        self.progress.emit("Probing source videos...")
        metadatas: list[VideoMetadata] = []
        for v in self._selected_videos:
            if self._cancel:
                self.finished_error.emit("Cancelled")
                return
            self.log.emit(f"Probing {v.name}...")
            meta = extract_metadata(v.path)
            if meta is None:
                self.log.emit(f"WARNING: ffprobe failed for {v.name}")
                meta = VideoMetadata(
                    source_filename=v.name,
                    source_path=v.path,
                )
            metadatas.append(meta)
        self.log.emit(f"Probed {len(metadatas)} video(s).")

        if self._cancel:
            self.finished_error.emit("Cancelled")
            return

        # --- Phase 2: Assemble context markdown ---
        self.progress.emit("Assembling context...")
        doc = _load_assembled_from_state(self._context_store, self._selected_videos)
        context_md = build_context_markdown(
            project_ctx=doc.project_context,
            video_sections=doc.videos,
            video_metadatas=metadatas,
            working_folder=self._working_folder,
        )
        self.log.emit(f"Context assembled ({len(context_md)} chars).")

        if self._cancel:
            self.finished_error.emit("Cancelled")
            return

        # --- Phase 3: Load storyboard ---
        storyboard_md = load_latest_storyboard(self._working_folder)
        if not storyboard_md.strip():
            self.finished_error.emit(
                "No storyboard found. Generate a storyboard in Stage 7 first."
            )
            return

        # --- Phase 4: Build prompt ---
        if self._is_refinement and self._existing_edit_plan_json:
            self.progress.emit("Preparing refinement...")
            user_prompt = build_refinement_prompt(
                self._feedback, self._existing_edit_plan_json,
                storyboard_md, context_md,
            )
            system_prompt = EDITING_SYSTEM_PROMPT + "\n\n" + _REFINEMENT_SUFFIX
        else:
            self.progress.emit("Preparing editing agent...")
            user_prompt = build_generation_prompt(storyboard_md, context_md)
            system_prompt = EDITING_SYSTEM_PROMPT

        # Add render preset instruction to the prompt
        user_prompt += (
            f"\n\n## Render Preset\n\n"
            f"Use preset='{self._render_preset}' for the final render."
        )

        if self._cancel:
            self.finished_error.emit("Cancelled")
            return

        # --- Phase 5: Build ToolRegistry ---
        self.progress.emit("Building tools...")
        registry = ToolRegistry(
            working_folder=self._working_folder,
            selected_videos=self._selected_videos,
            metadatas=metadatas,
        )
        tools = registry.get_tools()
        self.log.emit(f"Built {len(tools)} tools.")

        # --- Phase 6: Run agent loop (two-phase: plan → execute) ---
        self.progress.emit("Running editing agent...")

        def _is_cancelled() -> bool:
            return self._cancel

        final_text, tool_log = run_editing_agent(
            client=client,
            model=config.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=registry,
            progress_cb=lambda msg: self.progress.emit(msg),
            log_cb=lambda msg: self.log.emit(msg),
            is_cancelled=_is_cancelled,
        )

        if self._cancel:
            self.finished_error.emit("Cancelled")
            return

        self.log.emit(f"Agent finished. {len(tool_log)} tool calls made.")
        self.log.emit(f"Agent response: {final_text[:200]}...")

        # --- Phase 7: Persist edit plan + tool log ---
        self.progress.emit("Saving edit plan...")
        history = load_history(self._working_folder)
        storyboard_version = history.latest.version if history.latest else 0

        # Primary path: the agent committed a plan during the run (it is
        # already persisted to disk by commit_edit_plan / update_edit_plan).
        # Reload it from disk so we capture the latest state (including any
        # update_edit_plan amendments). Fall back to reconstructing from the
        # tool log only if the agent never committed a plan.
        edit_plan = load_edit_plan(self._working_folder)
        if edit_plan is None:
            edit_plan = build_edit_plan_from_tool_log(
                tool_log,
                storyboard_version=storyboard_version,
            )
            edit_plan.notes = final_text
            edit_plan.preset = self._render_preset
            save_edit_plan(self._working_folder, edit_plan)
        else:
            # Keep the committed plan authoritative; just record notes/preset.
            edit_plan.notes = final_text
            if self._render_preset:
                edit_plan.preset = self._render_preset
            save_edit_plan(self._working_folder, edit_plan)

        save_tool_log(self._working_folder, tool_log)
        self.log.emit("Saved edit plan and tool log.")

        self.progress.emit("Done")
        self.finished_success.emit(edit_plan)


_REFINEMENT_SUFFIX = (
    "You are refining an existing edit. Apply the user's feedback to "
    "modify the current edit plan rather than starting from scratch."
)


def _load_assembled_from_state(context_store: ContextStore, selected_videos: list):
    """Load the assembled context document from the ContextStore."""
    from types import SimpleNamespace
    state = SimpleNamespace(selected_videos=selected_videos)
    return load_assembled(state, context_store)