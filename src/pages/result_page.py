"""Stage 9 — Result screen.

Displays the final rendered video in a player with play/pause, seek, and
volume controls, alongside the agent's markdown summary report. Auto-navigated
to from Stage 8 only when a video was successfully rendered.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..context_review import markdown_to_html
from ..icons import material_icon
from ..theme import (
    COLOR_BORDER,
    COLOR_ON_SURFACE_VARIANT,
    COLOR_PRIMARY,
    COLOR_SURFACE,
    COLOR_SURFACE_CONTAINER,
    RADIUS_LG,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
)
from ..video_production import find_rendered_video, load_edit_plan
from .context_review_page import _AutoTextBrowser
from .video_preview import _format_time


class ResultPage(QWidget):
    """Stage 9 — show the final rendered video + the agent's report."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._video_path: Path | None = None
        self._player: QMediaPlayer | None = None
        self._audio_output: QAudioOutput | None = None
        self._build()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        header = QHBoxLayout()
        title = QLabel("Result")
        title.setProperty("class", "headline-md")
        header.addWidget(title)
        header.addStretch()
        self.status_label = QLabel("")
        self.status_label.setProperty("class", "label-sm")
        header.addWidget(self.status_label)
        root.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(SPACING_MD)
        self.scroll.setWidget(self._content)
        root.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        self.back_btn = QPushButton("Back to Production")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(lambda: self._state.set_stage(8))
        footer.addWidget(self.back_btn)
        root.addLayout(footer)

    # --- Lifecycle ---------------------------------------------------------
    def on_enter(self) -> None:
        """Called when navigating to this stage. Loads the video + report."""
        if not self._state.working_folder:
            return
        self._load_video()
        self._build_content()

    def _load_video(self) -> None:
        """Resolve the final video path from the edit plan or output dir."""
        self._video_path = None
        wf = self._state.working_folder
        if not wf:
            return
        # Prefer the edit plan's output_path (set by the worker post-render).
        plan = load_edit_plan(wf)
        if plan is not None and plan.output_path:
            p = Path(plan.output_path)
            if p.exists() and p.suffix.lower() == ".mp4":
                self._video_path = p
                return
        # Fall back to scanning the output directory for the newest render.
        found = find_rendered_video(wf)
        if found is not None:
            self._video_path = found

    # --- Content building --------------------------------------------------
    def _build_content(self) -> None:
        """Clear and rebuild the scrolling content."""
        # Stop any existing player before rebuilding.
        self._stop_player()

        self.report_browser: QTextBrowser | None = None

        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if self._video_path is None or not self._video_path.exists():
            self._content_layout.addWidget(self._build_empty_state())
            self._content_layout.addStretch()
            self.status_label.setText("")
            return

        self._content_layout.addWidget(self._build_player_card())
        self._content_layout.addWidget(self._build_report_card())
        self._content_layout.addStretch()
        self.status_label.setText(self._video_path.name)

    def _build_empty_state(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        icon = material_icon("movie", 56, COLOR_ON_SURFACE_VARIANT)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)

        lbl = QLabel("No rendered video found.")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setProperty("class", "body-md")
        lbl.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        lay.addWidget(lbl)

        go_btn = QPushButton("Go to Production")
        go_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        go_btn.clicked.connect(lambda: self._state.set_stage(8))
        lay.addWidget(go_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        return card

    def _build_player_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Final Video")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        # --- Video surface ---
        self._video_widget = QVideoWidget()
        self._video_widget.setStyleSheet(
            f"background-color: #000000; "
            f"border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {RADIUS_LG}px;"
        )
        self._video_widget.setMinimumHeight(320)
        lay.addWidget(self._video_widget, 1)

        # --- Player + audio output ---
        self._audio_output = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoOutput(self._video_widget)
        self._player.setSource(QUrl.fromLocalFile(str(self._video_path)))
        # Default volume to 100%.
        self._audio_output.setVolume(1.0)

        # --- Controls row ---
        controls = QHBoxLayout()
        controls.setSpacing(SPACING_SM)

        self.play_btn = QPushButton("Play")
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setFixedWidth(80)
        self.play_btn.clicked.connect(self._on_play_pause)
        controls.addWidget(self.play_btn)

        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.seek_slider.sliderMoved.connect(self._on_seek)
        controls.addWidget(self.seek_slider, 1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("class", "label-sm")
        self.time_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        self.time_label.setFixedWidth(96)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        controls.addWidget(self.time_label)

        # Volume control
        vol_icon = material_icon("volume_up", 18, COLOR_ON_SURFACE_VARIANT)
        controls.addWidget(vol_icon)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(96)
        self.volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        controls.addWidget(self.volume_slider)

        lay.addLayout(controls)

        # --- Wire player signals ---
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)

        return card

    def _build_report_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Final Video Report")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        # Render the agent's markdown notes (the summary it produced at the
        # end of the production run) as rendered HTML — not raw JSON.
        notes = ""
        plan = load_edit_plan(self._state.working_folder)
        if plan is not None and plan.notes:
            notes = plan.notes

        if notes.strip():
            html = markdown_to_html(notes)
        else:
            html = "<i>No report available.</i>"

        self.report_browser = _AutoTextBrowser()
        self.report_browser.setOpenExternalLinks(False)
        self.report_browser.setProperty("class", "body-sm")
        self.report_browser.setStyleSheet(
            f"QTextBrowser {{ background-color: {COLOR_SURFACE}; "
            f"border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {RADIUS_LG}px; "
            f"padding: {SPACING_MD}px; }}"
        )
        self.report_browser.setHtml(html)
        lay.addWidget(self.report_browser)
        return card

    # --- Player controls ---------------------------------------------------
    def _on_play_pause(self) -> None:
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self.play_btn.setText("Play")
        else:
            self._player.play()
            self.play_btn.setText("Pause")

    def _on_position_changed(self, position: int) -> None:
        if self.seek_slider is None:
            return
        # Avoid feedback loop: only update the slider if the user isn't
        # currently dragging it.
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        duration = self._player.duration() if self._player else 0
        self.time_label.setText(f"{_format_time(position)} / {_format_time(duration)}")

    def _on_duration_changed(self, duration: int) -> None:
        if self.seek_slider is not None:
            self.seek_slider.setRange(0, duration)

    def _on_seek(self, position: int) -> None:
        if self._player is not None:
            self._player.setPosition(position)

    def _on_volume_changed(self, value: int) -> None:
        if self._audio_output is not None:
            self._audio_output.setVolume(value / 100.0)

    def _stop_player(self) -> None:
        """Stop and release the current player before rebuilding content."""
        if self._player is not None:
            self._player.stop()
            self._player = None
        self._audio_output = None

    # --- Cleanup -----------------------------------------------------------
    def __del__(self):
        try:
            self._stop_player()
        except Exception:
            pass