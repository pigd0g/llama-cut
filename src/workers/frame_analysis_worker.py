from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..context import ContextStore, ContextType
from ..frame_analysis import (
    FrameAnalysisSettings,
    analyse_frame,
    append_sections,
    build_ollama_client,
    format_section,
    is_config_valid,
    load_ollama_config,
)
from ..state import Frame


class FrameAnalysisWorker(QThread):
    """Run Ollama vision analysis on the selected frames.

    For each video (in selection order), all of its selected frames are
    submitted to a ThreadPoolExecutor in chronological (pts_time) order. The
    pool runs at ``settings.concurrency`` workers. Sections are collected in
    submission order (not completion order) and appended to the video's
    Frame Analysis context file as a single write.

    A single ``ollama.Client`` is built once and shared across all calls in
    the pool (the SDK is httpx-backed and thread-safe).
    """
    progress = pyqtSignal(int, int, str)         # done, total, message
    frame_finished = pyqtSignal(object, str)     # Frame, section_text
    log = pyqtSignal(str)
    finished_all = pyqtSignal(bool)              # any_failed

    def __init__(self, frames: list[Frame], settings: FrameAnalysisSettings,
                 project_context: str,
                 video_contexts: dict[str, str],
                 context_store: ContextStore, parent=None):
        super().__init__(parent)
        self._frames = list(frames)
        self._settings = settings
        self._project_context = project_context
        # stem -> video markdown context (already loaded by the page)
        self._video_contexts = dict(video_contexts)
        self._context_store = context_store
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total = len(self._frames)
        if total == 0:
            self.progress.emit(0, 0, "No frames to analyse.")
            self.finished_all.emit(False)
            return

        # Validate Ollama config up front so we fail fast with a clear message.
        config = load_ollama_config()
        ok, msg = is_config_valid(config)
        if not ok:
            self.log.emit(msg)
            self.progress.emit(total, total, "Configuration missing")
            self.finished_all.emit(True)
            return

        self.log.emit(
            f"Connecting to Ollama at {config.host} (model: {config.model})..."
        )
        try:
            client = build_ollama_client(config)
        except Exception as e:
            self.log.emit(f"Failed to build Ollama client: {e}")
            self.progress.emit(total, total, "Connection failed")
            self.finished_all.emit(True)
            return

        concurrency = max(1, min(int(self._settings.concurrency or 1), 32))
        self.log.emit(f"Concurrency: {concurrency}")

        # Group frames by video_path preserving first-seen order.
        groups: list[tuple[str, str, list[Frame]]] = []  # (path, stem, frames)
        seen: dict[str, int] = {}
        for f in self._frames:
            if f.video_path not in seen:
                seen[f.video_path] = len(groups)
                groups.append((f.video_path, f.video_stem, []))
            groups[seen[f.video_path]][2].append(f)

        any_failed = False
        done = 0
        run_ts = datetime.now().isoformat(timespec="seconds")

        for video_path, stem, vframes in groups:
            if self._cancel:
                break
            # Chronological order within the video.
            vframes.sort(key=lambda fr: fr.pts_time)
            self.log.emit(f"=== {Path(video_path).name} ({len(vframes)} frames) ===")
            self.progress.emit(done, total, f"Analysing {Path(video_path).name}")

            existing = ""
            doc = self._context_store.get(stem, ContextType.FRAME_ANALYSIS)
            if doc is not None:
                existing = doc.content or ""

            video_ctx = self._video_contexts.get(stem, "")
            sections: list[str] = []

            def _do_one(frame: Frame) -> str:
                return analyse_frame(
                    client, config.model, frame.path,
                    self._project_context, video_ctx,
                    Path(video_path).name, frame,
                )

            try:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = [pool.submit(_do_one, fr) for fr in vframes]
                    for i, fut in enumerate(futures):
                        if self._cancel:
                            # Allow in-flight tasks to finish before exiting.
                            break
                        try:
                            text = fut.result()
                        except Exception as e:
                            text = ""
                            any_failed = True
                            self.log.emit(f"FAILED {vframes[i].filename}: {e}")
                            # Still emit a section so the file has an entry.
                            sections.append(
                                format_section(
                                    vframes[i],
                                    f"_ERROR: analysis failed — {e}_",
                                )
                            )
                            self.frame_finished.emit(vframes[i], sections[-1])
                            done += 1
                            self.progress.emit(done, total, f"Frame {i+1}/{len(vframes)} failed")
                            continue
                        sec = format_section(vframes[i], text)
                        sections.append(sec)
                        self.frame_finished.emit(vframes[i], sec)
                        done += 1
                        self.progress.emit(
                            done, total,
                            f"Frame {i+1}/{len(vframes)} of {Path(video_path).name}",
                        )
            except Exception as e:
                any_failed = True
                self.log.emit(f"Pool error: {e}")

            if self._cancel and not sections:
                # Cancelled before any frame in this video completed — skip write.
                continue

            # Append + write the context file for this video.
            try:
                content = append_sections(existing, run_ts, sections)
                self._context_store.save(stem, ContextType.FRAME_ANALYSIS, content)
                self.log.emit(
                    f"Wrote frame analysis context for {Path(video_path).name} "
                    f"({len(sections)} frames)."
                )
            except Exception as e:
                any_failed = True
                self.log.emit(f"FAILED to write context for {stem}: {e}")

        self.progress.emit(total, total, "Done")
        self.finished_all.emit(any_failed)