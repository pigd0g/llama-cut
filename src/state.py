from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .frame_analysis import FrameAnalysisSettings
from .storyboard import StoryboardSettings
from .transcription import TranscriptionSettings, model_cache_dir
from .video_production import VideoProductionSettings


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}

# Audio files in the working folder can be used as background music by the
# editor (Stage 8) via mix_audio. They are NOT selectable as video sources in
# Stage 1 — they are surfaced to the storyboard/editor via the context and
# resolved by _resolve_clip from the working folder root.
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac"}


def _sanitize_stem(stem: str) -> str:
    """Make a filename-safe stem (no path separators or illegal chars)."""
    out = []
    for ch in stem:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    return s[:120] if len(s) > 120 else s


@dataclass
class Video:
    path: str
    name: str
    stem: str
    size_bytes: int
    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    fps: float = 0.0
    thumbnail_path: str = ""
    selected: bool = False
    probed: bool = False

    @classmethod
    def from_path(cls, p: Path) -> "Video":
        return cls(
            path=str(p),
            name=p.name,
            stem=_sanitize_stem(p.stem),
            size_bytes=p.stat().st_size if p.exists() else 0,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Video":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items() if k in d})


@dataclass
class Frame:
    path: str
    filename: str
    video_path: str
    video_stem: str
    pts_time: float
    index: int
    strategy: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Frame":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items() if k in d})


@dataclass
class ExtractSettings:
    mode: str = "dynamic"  # dynamic | quick | standard | detailed | custom
    custom_count: int = 60

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractSettings":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items() if k in d})


class PipelineState(QObject):
    working_folder_changed = pyqtSignal(str)
    videos_changed = pyqtSignal()
    selection_changed = pyqtSignal()
    videos_probed = pyqtSignal()
    settings_changed = pyqtSignal()
    extraction_started = pyqtSignal()
    extraction_progress = pyqtSignal(int, str)
    extraction_finished = pyqtSignal(bool, str)
    frames_changed = pyqtSignal()
    stage_changed = pyqtSignal(int)
    transcription_settings_changed = pyqtSignal()
    frame_analysis_settings_changed = pyqtSignal()
    storyboard_settings_changed = pyqtSignal()
    video_production_settings_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._working_folder: str = ""
        self._videos: list[Video] = []
        self._settings = ExtractSettings()
        self._transcription_settings = TranscriptionSettings()
        self._frame_analysis_settings = FrameAnalysisSettings()
        self._storyboard_settings = StoryboardSettings()
        self._video_production_settings = VideoProductionSettings()
        self._frames: list[Frame] = []
        self._stage: int = 0  # 0=welcome, 1..5 = stages

    # --- Working folder -----------------------------------------------------
    @property
    def working_folder(self) -> str:
        return self._working_folder

    def set_working_folder(self, folder: str) -> None:
        if folder and folder != self._working_folder:
            self._frames.clear()
            self._videos.clear()
            self.frames_changed.emit()
            self.videos_changed.emit()
        self._working_folder = folder
        self.working_folder_changed.emit(folder)
        self.persist()

    @property
    def temp_dir(self) -> Path:
        if not self._working_folder:
            return Path()
        return Path(self._working_folder) / "temp"

    # --- Videos -------------------------------------------------------------
    @property
    def videos(self) -> list[Video]:
        return self._videos

    def set_videos(self, videos: list[Video]) -> None:
        self._videos = videos
        self.videos_changed.emit()
        self.selection_changed.emit()
        self.persist()

    @property
    def selected_videos(self) -> list[Video]:
        return [v for v in self._videos if v.selected]

    def set_selection(self, paths: set[str], selected: bool) -> None:
        for v in self._videos:
            if v.path in paths:
                v.selected = selected
        self.selection_changed.emit()
        self.persist()

    def select_all(self) -> None:
        for v in self._videos:
            v.selected = True
        self.selection_changed.emit()
        self.persist()

    def deselect_all(self) -> None:
        for v in self._videos:
            v.selected = False
        self.selection_changed.emit()
        self.persist()

    def update_video(self, video: Video) -> None:
        for i, v in enumerate(self._videos):
            if v.path == video.path:
                self._videos[i] = video
                return
        self._videos.append(video)

    def mark_probed(self) -> None:
        self.videos_probed.emit()
        self.persist()

    # --- Settings -----------------------------------------------------------
    @property
    def settings(self) -> ExtractSettings:
        return self._settings

    def set_settings(self, settings: ExtractSettings) -> None:
        self._settings = settings
        self.settings_changed.emit()
        self.persist()

    # --- Transcription settings --------------------------------------------
    @property
    def transcription_settings(self) -> TranscriptionSettings:
        return self._transcription_settings

    def set_transcription_settings(self, settings: TranscriptionSettings) -> None:
        self._transcription_settings = settings
        self.transcription_settings_changed.emit()
        self.persist()

    @property
    def frame_analysis_settings(self) -> FrameAnalysisSettings:
        return self._frame_analysis_settings

    def set_frame_analysis_settings(self, settings: FrameAnalysisSettings) -> None:
        self._frame_analysis_settings = settings
        self.frame_analysis_settings_changed.emit()
        self.persist()

    @property
    def storyboard_settings(self) -> StoryboardSettings:
        return self._storyboard_settings

    def set_storyboard_settings(self, settings: StoryboardSettings) -> None:
        self._storyboard_settings = settings
        self.storyboard_settings_changed.emit()
        self.persist()

    @property
    def video_production_settings(self) -> VideoProductionSettings:
        return self._video_production_settings

    def set_video_production_settings(self, settings: VideoProductionSettings) -> None:
        self._video_production_settings = settings
        self.video_production_settings_changed.emit()
        self.persist()

    @property
    def models_dir(self) -> Path:
        return model_cache_dir()

    # --- Frames -------------------------------------------------------------
    @property
    def frames(self) -> list[Frame]:
        return self._frames

    def set_frames(self, frames: list[Frame]) -> None:
        self._frames = frames
        self.frames_changed.emit()
        self.persist()

    @property
    def selected_frames(self) -> list[Frame]:
        # selection for frames is tracked in the page via a set; this is a stub
        return self._frames

    # --- Stage --------------------------------------------------------------
    @property
    def stage(self) -> int:
        return self._stage

    def set_stage(self, stage: int, force: bool = False) -> None:
        if stage == self._stage and not force:
            return
        self._stage = stage
        self.stage_changed.emit(stage)
        self.persist()

    # --- Persistence --------------------------------------------------------
    @property
    def _state_file(self) -> Path:
        return self.temp_dir / "app_state.json"

    def persist(self) -> None:
        if not self._working_folder:
            return
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "working_folder": self._working_folder,
                "stage": self._stage,
                "settings": self._settings.to_dict(),
                "transcription_settings": self._transcription_settings.to_dict(),
                "frame_analysis_settings": self._frame_analysis_settings.to_dict(),
                "storyboard_settings": self._storyboard_settings.to_dict(),
                "video_production_settings": self._video_production_settings.to_dict(),
                "videos": [v.to_dict() for v in self._videos],
                "frames": [f.to_dict() for f in self._frames],
            }
            self._state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load(self) -> None:
        if not self._working_folder:
            return
        f = self._state_file
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            self._stage = int(data.get("stage", 0))
            self._settings = ExtractSettings.from_dict(data.get("settings", {}))
            self._transcription_settings = TranscriptionSettings.from_dict(
                data.get("transcription_settings", {})
            )
            self._frame_analysis_settings = FrameAnalysisSettings.from_dict(
                data.get("frame_analysis_settings", {})
            )
            self._storyboard_settings = StoryboardSettings.from_dict(
                data.get("storyboard_settings", {})
            )
            self._video_production_settings = VideoProductionSettings.from_dict(
                data.get("video_production_settings", {})
            )
            self._videos = [Video.from_dict(d) for d in data.get("videos", [])]
            self._frames = [Frame.from_dict(d) for d in data.get("frames", [])]
            self.videos_changed.emit()
            self.frames_changed.emit()
            self.settings_changed.emit()
            self.transcription_settings_changed.emit()
            self.frame_analysis_settings_changed.emit()
            self.storyboard_settings_changed.emit()
            self.video_production_settings_changed.emit()
        except Exception:
            pass

    def load_frames_json(self) -> Optional[list[Frame]]:
        f = self.temp_dir / "frames.json"
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return [Frame.from_dict(d) for d in data.get("frames", [])]
        except Exception:
            return None

    def save_frames_json(self, frames: list[Frame]) -> None:
        if not self._working_folder:
            return
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            (self.temp_dir / "frames.json").write_text(
                json.dumps({"frames": [f.to_dict() for f in frames]}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass