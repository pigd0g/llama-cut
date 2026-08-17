from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal


class ThumbnailWorker(QThread):
    """Generate a single thumbnail per video in the background."""
    progress = pyqtSignal(int, int, str)  # done, total, video_path
    thumb_ready = pyqtSignal(str, str)    # video_path, thumbnail_path
    finished_all = pyqtSignal()

    def __init__(self, video_paths: list[str], thumbs_dir: Path, parent=None):
        super().__init__(parent)
        self._paths = list(video_paths)
        self._thumbs_dir = thumbs_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)
        total = len(self._paths)
        for i, vp in enumerate(self._paths):
            if self._cancel:
                return
            self.progress.emit(i, total, vp)
            thumb = self._generate(vp)
            if thumb:
                self.thumb_ready.emit(vp, thumb)
        self.progress.emit(total, total, "")
        self.finished_all.emit()

    def _generate(self, video_path: str) -> str | None:
        p = Path(video_path)
        thumb_path = self._thumbs_dir / f"{p.stem}.jpg"
        if thumb_path.exists() and thumb_path.stat().st_size > 0:
            return str(thumb_path)
        # seek to ~10% in to avoid black leader frames; -ss before -i is fast
        # we don't know duration yet, so use a fixed small offset
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [
            ffmpeg, "-hide_banner", "-y",
            "-ss", "1",
            "-i", str(p),
            "-frames:v", "1",
            "-vf", "scale=320:-2",
            "-q:v", "5",
            str(thumb_path),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                check=False, encoding="utf-8", errors="replace",
            )
            if proc.returncode == 0 and thumb_path.exists():
                return str(thumb_path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        # second attempt: no seek
        cmd2 = [
            ffmpeg, "-hide_banner", "-y",
            "-i", str(p),
            "-frames:v", "1",
            "-vf", "scale=320:-2",
            "-q:v", "5",
            str(thumb_path),
        ]
        try:
            proc = subprocess.run(
                cmd2, capture_output=True, text=True, timeout=60,
                check=False, encoding="utf-8", errors="replace",
            )
            if proc.returncode == 0 and thumb_path.exists():
                return str(thumb_path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None