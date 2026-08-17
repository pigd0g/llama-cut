from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from ..ffmpeg.probe import run_ffprobe
from ..state import Video


class ProbeWorker(QThread):
    """Run ffprobe over each selected video in the background."""
    progress = pyqtSignal(int, int, str)   # done, total, video_path
    video_probed = pyqtSignal(object)      # Video
    finished_all = pyqtSignal()

    def __init__(self, videos: list[Video], parent=None):
        super().__init__(parent)
        self._videos = list(videos)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total = len(self._videos)
        for i, v in enumerate(self._videos):
            if self._cancel:
                return
            self.progress.emit(i, total, v.path)
            result = run_ffprobe(v.path)
            if result:
                v.duration = result.duration
                v.width = result.width
                v.height = result.height
                v.codec = result.codec
                v.fps = result.fps
                v.probed = True
            self.video_probed.emit(v)
        self.progress.emit(total, total, "")
        self.finished_all.emit()