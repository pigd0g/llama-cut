from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..context import ContextStore, ContextType
from ..state import Video
from ..transcription import (
    TranscriptionResult,
    TranscriptionSettings,
    build_whisper,
    cuda_runtime_error_hint,
    extract_audio,
    segments_to_markdown,
    transcribe_audio,
)


class TranscriptionWorker(QThread):
    """Run the two-phase transcription pipeline for the selected videos.

    Phase 1: ffmpeg extracts audio to temp/<stem>.wav
    Phase 2: faster_whisper transcribes the wav
    The WAV is deleted after each video's transcription completes, and the
    resulting markdown is written to the video's Transcription Context file.
    """
    progress = pyqtSignal(int, int, str)         # done, total, message
    video_started = pyqtSignal(str)              # video_path
    video_finished = pyqtSignal(object, object)  # Video, TranscriptionResult
    log = pyqtSignal(str)
    finished_all = pyqtSignal(bool)              # any_failed

    def __init__(self, videos: list[Video], settings: TranscriptionSettings,
                 models_dir: Path, temp_dir: Path, context_store: ContextStore,
                 parent=None):
        super().__init__(parent)
        self._videos = list(videos)
        self._settings = settings
        self._models_dir = models_dir
        self._temp_dir = temp_dir
        self._context_store = context_store
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total = len(self._videos)
        any_failed = False

        # Load the model once for all videos.
        self.log.emit(
            f"Loading model {self._settings.model} on "
            f"{self._settings.device} ({self._settings.compute_type})..."
        )
        try:
            model = build_whisper(
                self._settings.model, self._models_dir,
                self._settings.device, self._settings.compute_type,
            )
        except Exception as e:
            msg = str(e)
            self.log.emit(f"Model load failed: {msg}")
            hint = cuda_runtime_error_hint(msg)
            if hint:
                self.log.emit(hint)
            self.progress.emit(total, total, "Model load failed")
            self.finished_all.emit(True)
            return
        self.log.emit("Model loaded.")

        for i, v in enumerate(self._videos):
            if self._cancel:
                break
            self.progress.emit(i, total, f"Transcribing {v.name}")
            self.video_started.emit(v.path)
            self.log.emit(f"=== {v.name} ===")

            wav_path = self._temp_dir / f"{v.stem}.wav"
            result = TranscriptionResult(video_path=v.path, video_stem=v.stem)
            try:
                self.log.emit("Extracting audio...")
                extract_audio(v.path, wav_path)
                self.log.emit("Transcribing audio...")
                result = transcribe_audio(model, wav_path, self._settings)
                result.video_path = v.path
                result.video_stem = v.stem
                self.log.emit(
                    f"Transcribed {len(result.segments)} segments "
                    f"(language: {result.detected_language or 'n/a'}, "
                    f"duration: {result.duration:.1f}s)"
                )
            except Exception as e:
                result.failed = True
                result.error = str(e)
                any_failed = True
                self.log.emit(f"FAILED: {e}")
                hint = cuda_runtime_error_hint(str(e))
                if hint:
                    self.log.emit(hint)
            finally:
                # delete the WAV after each transcription (per spec)
                try:
                    if wav_path.exists():
                        wav_path.unlink()
                except OSError:
                    pass

            # write the transcription markdown to the video's context file
            if not result.failed:
                try:
                    md = segments_to_markdown(result)
                    self._context_store.save(
                        v.stem, ContextType.TRANSCRIPTION, md,
                    )
                    self.log.emit("Wrote transcription context.")
                except Exception as e:
                    result.failed = True
                    result.error = f"context write failed: {e}"
                    any_failed = True
                    self.log.emit(f"FAILED to write context: {e}")

            self.video_finished.emit(v, result)

        self.progress.emit(total, total, "Done")
        self.finished_all.emit(any_failed)