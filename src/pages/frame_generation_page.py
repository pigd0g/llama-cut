from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ffmpeg.extract import select_strategy
from ..state import ExtractSettings
from ..theme import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
)
from ..workers.extract_worker import ExtractWorker
from ..workers.probe_worker import ProbeWorker


class FrameGenerationPage(QWidget):
    """Stage 2 — probe videos, choose strategy, run extraction."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._probe_worker: ProbeWorker | None = None
        self._extract_worker: ExtractWorker | None = None
        self._build()
        self._connect()

    # --- UI -----------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        outer.setSpacing(SPACING_MD)

        title = QLabel("Frame Generation")
        title.setProperty("class", "headline-md")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(SPACING_MD)

        cl.addWidget(self._build_summary_table())
        cl.addWidget(self._build_settings_card())
        cl.addWidget(self._build_advanced_card())
        cl.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        outer.addWidget(self._build_progress_block())

        footer = QHBoxLayout()
        footer.addStretch()
        self.back_btn = QPushButton("Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(lambda: self._state.set_stage(2))
        footer.addWidget(self.back_btn)
        self.analyse_btn = QPushButton("Analyse")
        self.analyse_btn.setProperty("class", "primary")
        self.analyse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.analyse_btn.clicked.connect(self._on_analyse)
        footer.addWidget(self.analyse_btn)
        outer.addLayout(footer)

    def _build_summary_table(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        hdr = QLabel("Videos")
        hdr.setProperty("class", "headline-sm")
        lay.addWidget(hdr)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["File", "Duration", "Resolution", "Codec", "Strategy"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(False)
        lay.addWidget(self.table)
        return card

    def _build_settings_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        hdr = QLabel("Frame Generation Setting")
        hdr.setProperty("class", "headline-sm")
        lay.addWidget(hdr)

        self.r_dynamic = QRadioButton("Dynamic (Recommended)")
        self.r_dynamic.setChecked(True)
        self.r_quick = QRadioButton("Quick  (~30 frames)")
        self.r_standard = QRadioButton("Standard  (~60 frames)")
        self.r_detailed = QRadioButton("Detailed  (~80 frames)")
        self.r_custom = QRadioButton("Custom")

        self.custom_spin = QSpinBox()
        self.custom_spin.setRange(1, 500)
        self.custom_spin.setValue(60)
        self.custom_spin.setEnabled(False)
        self.r_custom.toggled.connect(self.custom_spin.setEnabled)

        for r in (self.r_dynamic, self.r_quick, self.r_standard,
                  self.r_detailed, self.r_custom):
            lay.addWidget(r)
        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(SPACING_XL, 0, 0, 0)
        custom_row.addWidget(QLabel("Target frame count:"))
        custom_row.addWidget(self.custom_spin)
        custom_row.addStretch()
        lay.addLayout(custom_row)

        desc = QLabel(
            "Dynamic automatically picks the extraction strategy based on each "
            "video's duration. Quick / Standard / Detailed / Custom target a "
            "fixed frame count via evenly-spaced sampling."
        )
        desc.setWordWrap(True)
        desc.setProperty("class", "body-sm")
        lay.addWidget(desc)

        for r in (self.r_dynamic, self.r_quick, self.r_standard,
                  self.r_detailed, self.r_custom):
            r.toggled.connect(self._on_settings_changed)
        self.custom_spin.valueChanged.connect(self._on_settings_changed)
        return card

    def _build_advanced_card(self) -> QWidget:
        self.advanced_card = QFrame()
        self.advanced_card.setProperty("class", "card")
        lay = QVBoxLayout(self.advanced_card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        self.advanced_toggle = QCheckBox("Show FFmpeg strategy details (Advanced)")
        self.advanced_toggle.toggled.connect(self._on_advanced_toggled)
        lay.addWidget(self.advanced_toggle)

        self.advanced_body = QWidget()
        abl = QVBoxLayout(self.advanced_body)
        abl.setContentsMargins(SPACING_XL, 0, 0, 0)
        abl.setSpacing(SPACING_XS)
        abl.addWidget(QLabel(
            "Duration 0-60s: 1 frame / 2s\n"
            "1-10 min: scene detection (threshold 0.3)\n"
            "10-30 min: keyframe extraction\n"
            "30min+: thumbnail filter (total_frames / 60)\n\n"
            "Rules: scene detection yielding 0 frames retries at 1 frame / 5s. "
            "More than 100 extracted frames are subsampled to 80. Failures "
            "fall back toward simpler strategies."
        ))
        self.advanced_body.setVisible(False)
        lay.addWidget(self.advanced_body)
        return self.advanced_card

    def _build_progress_block(self) -> QWidget:
        block = QFrame()
        block.setProperty("class", "card")
        lay = QVBoxLayout(block)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        self.progress_label = QLabel("")
        self.progress_label.setProperty("class", "label-md")
        lay.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        lay.addWidget(self.progress_bar)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(120)
        self.log_box.setProperty("class", "body-sm")
        lay.addWidget(self.log_box)
        block.setVisible(False)
        self._progress_block = block
        return block

    # --- Signals ------------------------------------------------------------
    def _connect(self) -> None:
        self._state.videos_probed.connect(self._on_videos_probed)

    # --- Lifecycle ----------------------------------------------------------
    def on_enter(self) -> None:
        """Called when the user navigates to this stage."""
        # refresh summary table from current state
        self._populate_table()
        # probe if not already probed
        unprobed = [v for v in self._state.selected_videos if not v.probed]
        if unprobed and not (self._probe_worker and self._probe_worker.isRunning()):
            self._start_probe(unprobed)

    def _start_probe(self, videos) -> None:
        self.progress_label.setText("Probing videos...")
        self._progress_block.setVisible(True)
        self.progress_bar.setRange(0, len(videos))
        self.progress_bar.setValue(0)
        self._probe_worker = ProbeWorker(videos, self)
        self._probe_worker.progress.connect(self._on_probe_progress)
        self._probe_worker.video_probed.connect(self._on_video_probed)
        self._probe_worker.finished_all.connect(self._on_probe_done)
        self._probe_worker.start()

    def _on_probe_progress(self, done, total, _path) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)

    def _on_video_probed(self, video) -> None:
        self._state.update_video(video)
        self._populate_table()

    def _on_probe_done(self) -> None:
        self._state.mark_probed()
        self._progress_block.setVisible(False)

    def _on_videos_probed(self) -> None:
        self._populate_table()

    def _populate_table(self) -> None:
        videos = self._state.selected_videos
        self.table.setRowCount(0)
        for v in videos:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(v.name))
            self.table.setItem(row, 1, QTableWidgetItem(_duration_label(v.duration)))
            self.table.setItem(row, 2, QTableWidgetItem(
                f"{v.width}x{v.height}" if v.width else "—"))
            self.table.setItem(row, 3, QTableWidgetItem(v.codec or "—"))
            decision = select_strategy(
                self._current_mode(), v.duration, v.fps,
                self.custom_spin.value(),
            )
            self.table.setItem(row, 4, QTableWidgetItem(decision.label))
        self.table.resizeColumnsToContents()

    # --- Settings -----------------------------------------------------------
    def _current_mode(self) -> str:
        if self.r_dynamic.isChecked():
            return "dynamic"
        if self.r_quick.isChecked():
            return "quick"
        if self.r_standard.isChecked():
            return "standard"
        if self.r_detailed.isChecked():
            return "detailed"
        return "custom"

    def _on_settings_changed(self) -> None:
        self._state.set_settings(ExtractSettings(
            mode=self._current_mode(),
            custom_count=self.custom_spin.value(),
        ))
        self._populate_table()

    def _on_advanced_toggled(self, checked: bool) -> None:
        self.advanced_body.setVisible(checked)

    # --- Extraction ---------------------------------------------------------
    def _on_analyse(self) -> None:
        if self._extract_worker and self._extract_worker.isRunning():
            return
        # make sure settings are current
        self._on_settings_changed()
        videos = self._state.selected_videos
        if not videos:
            return
        self._progress_block.setVisible(True)
        self.progress_bar.setRange(0, len(videos))
        self.progress_bar.setValue(0)
        self.log_box.clear()
        self.analyse_btn.setEnabled(False)
        self._extract_worker = ExtractWorker(
            videos, self._state.settings, self._state.temp_dir, self,
        )
        self._extract_worker.progress.connect(self._on_extract_progress)
        self._extract_worker.log.connect(self._on_extract_log)
        self._extract_worker.video_finished.connect(self._on_video_extracted)
        self._extract_worker.finished_all.connect(self._on_extract_done)
        self._extract_worker.start()

    def _on_extract_progress(self, done, total, msg) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self.progress_label.setText(msg)

    def _on_extract_log(self, msg: str) -> None:
        self.log_box.appendPlainText(msg)

    def _on_video_extracted(self, video, outcome) -> None:
        pass  # progress handled by signal

    def _on_extract_done(self, any_failed: bool) -> None:
        self.analyse_btn.setEnabled(True)
        frames = self._extract_worker.all_frames if self._extract_worker else []
        self._state.set_frames(frames)
        self._state.save_frames_json(frames)
        if not any_failed and frames:
            self._state.set_stage(4)
        else:
            self.progress_label.setText(
                "Extraction completed with errors" if any_failed
                else "Extraction complete"
            )


def _duration_label(d: float) -> str:
    if d <= 0:
        return "—"
    h = int(d // 3600)
    m = int((d % 3600) // 60)
    s = int(d % 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"