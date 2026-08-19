from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from ..context import ContextStore
from ..context_review import load_assembled
from ..storyboard import (
    build_context_markdown,
    build_generation_prompt,
    build_ollama_client,
    build_refinement_prompt,
    generate_storyboard,
    is_config_valid,
    load_history,
    load_storyboard_config,
    refine_storyboard,
    save_history,
    save_latest_storyboard,
)
from ..video_metadata import VideoMetadata, extract_metadata


class StoryboardWorker(QThread):
    """Run the storyboard generation or refinement workflow.

    Phase 1: Run ffprobe (via extract_metadata) on every selected video.
    Phase 2: Assemble context markdown from the ContextStore + metadata.
    Phase 3: Call the Ollama workflow model (generate or refine).
    Phase 4: Persist the new version to history.json + storyboard.md.

    The worker does NOT own the history — it loads, appends, and saves it.
    The page owns the history for UI display.
    """

    # str — human-readable status message
    progress = pyqtSignal(str)
    # str — detailed log line
    log = pyqtSignal(str)
    # StoryboardVersion — the newly created version
    finished_success = pyqtSignal(object)
    # str — error message
    finished_error = pyqtSignal(str)

    def __init__(self, prompt: str, existing_storyboard: str | None,
                 working_folder: str, selected_videos: list,
                 context_store: ContextStore,
                 is_refinement: bool, parent=None):
        super().__init__(parent)
        self._prompt = prompt
        self._existing_storyboard = existing_storyboard
        self._working_folder = working_folder
        self._selected_videos = list(selected_videos)
        self._context_store = context_store
        self._is_refinement = is_refinement
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
        config = load_storyboard_config()
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

        # --- Phase 1: ffprobe on every selected video ---
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
                # Still add an entry with the filename so the LLM knows about
                # the file even if technical details are unavailable.
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
        doc = load_assembled_from_state(self._context_store, self._selected_videos)
        context_md = build_context_markdown(
            project_ctx=doc.project_context,
            video_sections=doc.videos,
            video_metadatas=metadatas,
        )
        self.log.emit(f"Context assembled ({len(context_md)} chars).")

        if self._cancel:
            self.finished_error.emit("Cancelled")
            return

        # --- Phase 3: Call Ollama ---
        if self._is_refinement and self._existing_storyboard:
            self.progress.emit("Refining storyboard...")
            prompt = build_refinement_prompt(
                self._prompt, self._existing_storyboard, context_md,
            )
            self.log.emit("Sending refinement request to LLM...")
            storyboard_md = refine_storyboard(client, config.model, prompt)
        else:
            self.progress.emit("Generating storyboard...")
            prompt = build_generation_prompt(self._prompt, context_md)
            self.log.emit("Sending generation request to LLM...")
            storyboard_md = generate_storyboard(client, config.model, prompt)

        storyboard_md = (storyboard_md or "").strip()
        if not storyboard_md:
            self.log.emit("LLM returned empty response.")
            self.finished_error.emit("The model returned an empty storyboard. Try again.")
            return
        self.log.emit(f"Received storyboard ({len(storyboard_md)} chars).")

        # --- Phase 4: Persist ---
        self.progress.emit("Saving storyboard...")
        history = load_history(self._working_folder)
        version = history.add(
            prompt=self._prompt,
            storyboard=storyboard_md,
            model=config.model,
            is_initial=not self._is_refinement,
        )
        save_history(self._working_folder, history)
        save_latest_storyboard(self._working_folder, storyboard_md)
        self.log.emit(
            f"Saved storyboard v{version.version} to storyboard/storyboard.md."
        )

        self.progress.emit("Done")
        self.finished_success.emit(version)


def load_assembled_from_state(context_store: ContextStore, selected_videos: list):
    """Load the assembled context document from the ContextStore.

    Thin wrapper around ``context_review.load_assembled`` that builds a
    minimal state-like object so the function can work without the full
    PipelineState.
    """
    from types import SimpleNamespace
    state = SimpleNamespace(selected_videos=selected_videos)
    return load_assembled(state, context_store)