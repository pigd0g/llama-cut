"""Reusable modal video preview + shared thumbnail context menu.

Provides:
  - :class:`VideoPlayerWidget` — embeddable video surface + transport controls.
  - :class:`VideoPreviewDialog` — modal wrapper with safe player cleanup on
    close (avoids the Windows multimedia backend deadlock when the video
    surface is still attached while the dialog is destroyed).
  - :func:`show_video_context_menu` — builds and execs a right-click menu
    with three actions: Preview video, Open in folder, Copy path. Used by
    every page that shows a video thumbnail.
  - :func:`_format_time` — ``M:SS`` / ``H:MM:SS`` formatter for timecodes.

The cleanup discipline is the same as the one used for the Result page:
disconnect signals -> detach the video surface -> clear the media source ->
stop -> deleteLater the player and audio output. Detaching the surface
*before* stopping is what actually avoids the Windows deadlock.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QIcon
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..icons import material_icon, material_icon_pixmap
from ..theme import (
    COLOR_BORDER,
    COLOR_ON_SURFACE,
    COLOR_ON_SURFACE_VARIANT,
    RADIUS_LG,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
)


def _format_time(ms: int) -> str:
    """Render a millisecond position as M:SS (or H:MM:SS for long videos)."""
    if ms < 0:
        ms = 0
    total_s = ms // 1000
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class VideoPlayerWidget(QWidget):
    """Reusable video monitor + transport controls.

    Embeds QVideoWidget, QMediaPlayer, play/pause, seek, volume, and timecode.
    Caller supplies the video path via set_video_path(). The widget owns the
    player lifecycle and stops playback when a new path is set or on cleanup.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._video_path: Path | None = None
        self._player: QMediaPlayer | None = None
        self._audio_output: QAudioOutput | None = None
        self._is_seeking = False
        self._build()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACING_SM)

        # Monitor surface
        self._video_widget = QVideoWidget()
        self._video_widget.setMinimumHeight(220)
        self._video_widget.setStyleSheet(
            f"background-color: #000000; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {RADIUS_LG}px;"
        )
        root.addWidget(self._video_widget, 1)

        # Transport row
        transport = QHBoxLayout()
        transport.setSpacing(SPACING_SM)

        self.play_btn = QPushButton()
        self.play_btn.setFixedSize(32, 32)
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setProperty("class", "ghost")
        self._set_play_icon("play_arrow")
        self.play_btn.clicked.connect(self._on_play_pause)
        transport.addWidget(self.play_btn)

        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.seek_slider.sliderPressed.connect(self._on_slider_pressed)
        self.seek_slider.sliderReleased.connect(self._on_slider_released)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        transport.addWidget(self.seek_slider, 1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("class", "label-sm")
        self.time_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        self.time_label.setFixedWidth(96)
        self.time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        transport.addWidget(self.time_label)

        vol_icon = material_icon("volume_up", 18, COLOR_ON_SURFACE_VARIANT)
        transport.addWidget(vol_icon)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(80)
        self.volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        transport.addWidget(self.volume_slider)

        root.addLayout(transport)

    def _set_play_icon(self, name: str) -> None:
        self.play_btn.setText(name)
        self.play_btn.setStyleSheet(
            f"font-family: 'Material Symbols Outlined'; font-size: 20px; "
            f"color: {COLOR_ON_SURFACE}; padding: 0px;"
        )

    # --- Player lifecycle --------------------------------------------------
    def set_video_path(self, path: str | Path | None) -> None:
        """Load a new video path. Stops and releases any existing player."""
        self._stop_player()
        if not path:
            self._video_path = None
            self.time_label.setText("0:00 / 0:00")
            self.seek_slider.setRange(0, 0)
            return

        p = Path(path)
        if not p.exists() or not p.is_file():
            self._video_path = None
            self.time_label.setText("No video")
            return

        self._video_path = p
        self._audio_output = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoOutput(self._video_widget)
        self._player.setSource(QUrl.fromLocalFile(str(p)))
        self._audio_output.setVolume(self.volume_slider.value() / 100.0)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._on_playback_state_changed(self._player.playbackState())
        # Start playback immediately when a video is loaded.
        self._player.play()

    def _stop_player(self) -> None:
        if self._player is not None:
            player = self._player
            self._player = None
            try:
                player.positionChanged.disconnect(self._on_position_changed)
                player.durationChanged.disconnect(self._on_duration_changed)
                player.playbackStateChanged.disconnect(
                    self._on_playback_state_changed
                )
            except Exception:
                pass
            try:
                # Detach the video surface before stopping; this avoids a
                # Windows backend deadlock when the parent widget is destroyed.
                player.setVideoOutput(None)
                player.setSource(QUrl())
                player.stop()
            except Exception:
                pass
            player.deleteLater()
        if self._audio_output is not None:
            audio = self._audio_output
            self._audio_output = None
            try:
                audio.setVolume(0.0)
            except Exception:
                pass
            audio.deleteLater()

    # --- Controls ----------------------------------------------------------
    def _on_play_pause(self) -> None:
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._set_play_icon("pause")
        else:
            self._set_play_icon("play_arrow")

    def _on_position_changed(self, position: int) -> None:
        if not self._is_seeking and self.seek_slider is not None:
            self.seek_slider.setValue(position)
        duration = self._player.duration() if self._player else 0
        self.time_label.setText(
            f"{_format_time(position)} / {_format_time(duration)}"
        )

    def _on_duration_changed(self, duration: int) -> None:
        if self.seek_slider is not None:
            self.seek_slider.setRange(0, duration)

    def _on_slider_pressed(self) -> None:
        self._is_seeking = True

    def _on_slider_released(self) -> None:
        self._is_seeking = False
        if self._player is not None:
            self._player.setPosition(self.seek_slider.value())

    def _on_seek_moved(self, position: int) -> None:
        duration = self._player.duration() if self._player else 0
        self.time_label.setText(
            f"{_format_time(position)} / {_format_time(duration)}"
        )

    def _on_volume_changed(self, value: int) -> None:
        if self._audio_output is not None:
            self._audio_output.setVolume(value / 100.0)

    # --- Cleanup -----------------------------------------------------------
    def cleanup(self) -> None:
        self._stop_player()

    def hideEvent(self, event) -> None:
        """Pause playback when the widget is hidden to avoid wasted decoding."""
        if self._player is not None:
            self._player.pause()
        super().hideEvent(event)


class VideoPreviewDialog(QDialog):
    """Modal video preview dialog wrapping :class:`VideoPlayerWidget`."""

    def __init__(self, video_path: str | Path, title: str = "Preview",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(640, 480)
        self.resize(880, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        self.player = VideoPlayerWidget(self)
        self.player.set_video_path(video_path)
        root.addWidget(self.player, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def closeEvent(self, event) -> None:
        """Stop playback safely before closing the modal.

        QMediaPlayer on Windows can deadlock if stop()/close() is called while
        the video surface is still attached to a visible widget. We detach
        and stop first, then allow the normal close to proceed.
        """
        if self.player is not None:
            self.player.cleanup()
            self.player.setParent(None)
            self.player.deleteLater()
            self.player = None
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self.close()
        super().done(result)


# --- Shared right-click context menu ----------------------------------------

def show_video_context_menu(video_path: str | Path, video_name: str,
                             global_pos, parent: QWidget) -> None:
    """Build and exec the thumbnail right-click menu.

    Actions:
      - Preview video   -> opens :class:`VideoPreviewDialog` (modal).
      - Open in folder  -> reveals the file in the OS file explorer.
      - Copy path       -> copies the absolute path to the clipboard.

    ``global_pos`` is the position at which to show the menu, in global
    screen coordinates (e.g. from ``event.globalPos()``). ``parent`` is the
    widget to parent both the menu and the preview dialog to.
    """
    path = Path(video_path)
    exists = path.is_file()

    menu = QMenu(parent)
    menu.setObjectName("videoContextMenu")

    # --- Preview video ---
    preview_act = menu.addAction(
        QIcon(material_icon_pixmap("play_arrow", 20, COLOR_ON_SURFACE)),
        "Preview video",
    )
    preview_act.setEnabled(exists)
    preview_act.triggered.connect(
        lambda: VideoPreviewDialog(path, title=video_name, parent=parent).exec()
    )

    menu.addSeparator()

    # --- Open in folder ---
    folder_act = menu.addAction(
        QIcon(material_icon_pixmap("folder_open", 20, COLOR_ON_SURFACE)),
        "Open in folder",
    )
    folder_act.setEnabled(exists)
    folder_act.triggered.connect(lambda: _reveal_in_file_manager(path))

    # --- Copy path ---
    copy_act = menu.addAction(
        QIcon(material_icon_pixmap("content_copy", 20, COLOR_ON_SURFACE)),
        "Copy path",
    )
    copy_act.setEnabled(exists)
    copy_act.triggered.connect(lambda: _copy_path(path))

    menu.exec(global_pos)


def _copy_path(path: Path) -> None:
    cb = QApplication.clipboard()
    if cb is not None:
        cb.setText(str(path.resolve()))


def _reveal_in_file_manager(path: Path) -> None:
    """Reveal ``path`` in the platform's file manager."""
    path = Path(path)
    if not path.exists():
        return
    if sys.platform.startswith("win"):
        # explorer /select,"<path>" selects the file in Explorer.
        import subprocess
        subprocess.Popen(
            ["explorer", "/select,", str(path.resolve())],
            shell=False,
        )
    elif sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["open", "-R", str(path.resolve())], shell=False)
    else:
        # Linux/BSD: no cross-DE "select file" verb, so just open the parent.
        from PyQt6.QtGui import QDesktopServices
        QDesktopServices.openUrl(path.parent.resolve().as_uri())