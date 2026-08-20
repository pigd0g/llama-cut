from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..ffmpeg.extract import (
    select_strategy,
    extract_frames,
)
from ..state import Frame, Video, ExtractSettings


class ExtractWorker(QThread):
    """Run frame extraction for every selected video sequentially."""
    progress = pyqtSignal(int, int, str)         # done, total, message
    video_started = pyqtSignal(str)              # video_path
    video_finished = pyqtSignal(object, object)  # Video, ExtractionOutcome
    log = pyqtSignal(str)
    finished_all = pyqtSignal(bool)              # any_failed

    def __init__(self, videos: list[Video], settings: ExtractSettings,
                 frames_dir: Path, parent=None):
        super().__init__(parent)
        self._videos = list(videos)
        self._settings = settings
        self._frames_dir = frames_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total = len(self._videos)
        any_failed = False
        all_frames: list[Frame] = []
        for i, v in enumerate(self._videos):
            if self._cancel:
                break
            self.progress.emit(i, total, f"Extracting {v.name}")
            self.video_started.emit(v.path)
            self.log.emit(f"=== {v.name} ===")

            decision = select_strategy(
                self._settings.mode, v.duration, v.fps,
                self._settings.custom_count,
            )
            self.log.emit(f"Strategy: {decision.label}")

            out_dir = self._frames_dir / v.stem
            outcome = extract_frames(
                video_path=v.path,
                video_stem=v.stem,
                duration=v.duration,
                fps=v.fps,
                out_dir=out_dir,
                decision=decision,
                progress_cb=lambda msg: self.log.emit(msg),
                is_cancelled=lambda: self._cancel,
            )

            if outcome.failed:
                any_failed = True
                self.log.emit(f"FAILED: {outcome.error}")
            else:
                self.log.emit(
                    f"Extracted {outcome.frame_count()} frames "
                    f"using {outcome.strategy_label}"
                )
                for rec in outcome.frames:
                    all_frames.append(Frame(
                        path=rec["path"],
                        filename=rec["filename"],
                        video_path=rec["video_path"],
                        video_stem=rec["video_stem"],
                        pts_time=rec["pts_time"],
                        index=rec["index"],
                        strategy=rec["strategy"],
                    ))

            self.video_finished.emit(v, outcome)

        self.progress.emit(total, total, "Done")
        self._all_frames = all_frames
        self.finished_all.emit(any_failed)

    @property
    def all_frames(self) -> list[Frame]:
        return getattr(self, "_all_frames", [])