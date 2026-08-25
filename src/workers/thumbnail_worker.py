from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

# Serialises thumbnail generation so two workers (e.g. a superseded one that
# was still running when the page was refreshed) can never write the same
# .jpg concurrently and corrupt it — the cause of a thumbnail that stays
# blank while every other video gets one.
_GENERATION_LOCK = threading.Lock()


def _is_valid_thumbnail(p: Path) -> bool:
    """A thumbnail is reusable only if it is a non-empty, decodable image.

    A partial/corrupt .jpg left by a crashed or cancelled ffmpeg must never
    be reported as ready — the page would show a permanently blank card.
    """
    try:
        if not p.exists() or p.stat().st_size == 0:
            return False
        img = QImage(str(p))
        return not img.isNull() and img.width() > 0 and img.height() > 0
    except OSError:
        return False


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
        self._proc: subprocess.Popen | None = None

    def cancel(self) -> None:
        self._cancel = True

    def kill(self) -> None:
        """Cancel and terminate the in-flight ffmpeg subprocess, if any.

        Used when a new worker supersedes this one. Without it the old
        ffmpeg keeps running and can race the new worker writing the same
        thumbnail file.
        """
        self._cancel = True
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

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
        # Serialize the whole generate+validate step per thumbnail so two
        # workers can never corrupt the same output file.
        with _GENERATION_LOCK:
            self._thumbs_dir.mkdir(parents=True, exist_ok=True)
            if _is_valid_thumbnail(thumb_path):
                return str(thumb_path)
            # A corrupt/partial thumbnail from an earlier run must not be
            # reused — delete it and regenerate.
            try:
                thumb_path.unlink(missing_ok=True)
            except OSError:
                pass
            # seek to ~10% in to avoid black leader frames; -ss before -i is
            # fast. Second attempt drops the seek for files where it fails.
            for cmd in (self._seek_cmd(p, thumb_path),
                        self._plain_cmd(p, thumb_path)):
                if self._cancel:
                    return None
                if self._run(cmd) and _is_valid_thumbnail(thumb_path):
                    return str(thumb_path)
                try:
                    thumb_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return None

    # --- ffmpeg commands ----------------------------------------------------

    def _seek_cmd(self, p: Path, thumb_path: Path) -> list[str]:
        return [self._ffmpeg(), "-hide_banner", "-nostdin", "-y",
                "-ss", "1", "-i", str(p),
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
                str(thumb_path)]

    def _plain_cmd(self, p: Path, thumb_path: Path) -> list[str]:
        return [self._ffmpeg(), "-hide_banner", "-nostdin", "-y",
                "-i", str(p),
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
                str(thumb_path)]

    @staticmethod
    def _ffmpeg() -> str:
        return shutil.which("ffmpeg") or "ffmpeg"

    def _run(self, cmd: list[str]) -> bool:
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            self._proc = None
            return False
        try:
            self._proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
                self._proc.communicate()
            except Exception:
                pass
            self._proc = None
            return False
        rc = self._proc.returncode
        self._proc = None
        return rc == 0
