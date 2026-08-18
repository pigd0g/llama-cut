from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..transcription import download_model


class ModelDownloadWorker(QThread):
    """Download a Whisper model into the project's models/ directory.

    Emits coarse per-stage messages (no byte-level progress). Designed to
    keep the UI responsive during a multi-hundred-MB download.
    """
    progress = pyqtSignal(str)        # human-readable status message
    finished_download = pyqtSignal(bool)  # success

    def __init__(self, model: str, cache_dir: Path, parent=None):
        super().__init__(parent)
        self._model = model
        self._cache_dir = cache_dir

    def run(self) -> None:
        # Silence the HuggingFace symlink warning on filesystems without
        # symlink support (e.g. some Windows setups). Caching still works.
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        ok = download_model(self._model, self._cache_dir,
                            progress_cb=self.progress.emit)
        self.finished_download.emit(ok)