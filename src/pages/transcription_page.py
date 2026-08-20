from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..context import ContextStore
from .. import paths
from ..theme import (
    COLOR_SUCCESS,
    COLOR_WARNING,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
)
from ..transcription import (
    SUPPORTED_MODELS,
    TranscriptionSettings,
    compute_type_options_for_device,
    default_compute_type_for_device,
    detect_hardware_accel,
    is_model_present,
)
from ..workers.model_download_worker import ModelDownloadWorker
from ..workers.transcription_worker import TranscriptionWorker
from .video_preview import show_video_context_menu
from .widgets import ThumbDelegate


class TranscriptionPage(QWidget):
    """Stage 3 — optional transcription via ffmpeg audio extraction +
    faster_whisper transcription."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._context_store: ContextStore | None = None
        self._download_worker: ModelDownloadWorker | None = None
        self._transcribe_worker: TranscriptionWorker | None = None
        self._hwaccel: str = "cpu"
        self._build()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        outer.setSpacing(SPACING_MD)

        title = QLabel("Transcription")
        title.setProperty("class", "headline-md")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(SPACING_MD)

        cl.addWidget(self._build_enable_card())
        cl.addWidget(self._build_videos_card())
        cl.addWidget(self._build_model_card())
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
        self.skip_btn = QPushButton("Skip Transcription")
        self.skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_btn.clicked.connect(self._on_skip)
        footer.addWidget(self.skip_btn)
        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.setProperty("class", "primary")
        self.transcribe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.transcribe_btn.clicked.connect(self._on_transcribe)
        footer.addWidget(self.transcribe_btn)
        outer.addLayout(footer)

    def _build_enable_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        self.enable_cb = QCheckBox("Enable transcription")
        self.enable_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.enable_cb.toggled.connect(self._on_enable_toggled)
        lay.addWidget(self.enable_cb)
        desc = QLabel(
            "Transcription is optional. Enable it to extract and transcribe "
            "audio for the selected videos. Skip to proceed without "
            "transcription."
        )
        desc.setWordWrap(True)
        desc.setProperty("class", "body-sm")
        lay.addWidget(desc)
        return card

    def _build_videos_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        hdr = QHBoxLayout()
        lbl = QLabel("Select videos to transcribe")
        lbl.setProperty("class", "headline-sm")
        hdr.addWidget(lbl)
        hdr.addStretch()
        self.count_label = QLabel("0 of 0 selected")
        self.count_label.setProperty("class", "label-md")
        hdr.addWidget(self.count_label)
        lay.addLayout(hdr)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(SPACING_SM)
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_btn.clicked.connect(self._on_select_all)
        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_none_btn.clicked.connect(self._on_select_none)
        toolbar.addWidget(self.select_all_btn)
        toolbar.addWidget(self.select_none_btn)
        toolbar.addStretch()
        lay.addLayout(toolbar)

        self.model = QStandardItemModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(ThumbDelegate(self.view))
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setSpacing(SPACING_MD)
        self.view.setUniformItemSizes(True)
        self.view.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.clicked.connect(self._on_item_clicked)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        lay.addWidget(self.view)
        return card

    def _build_model_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        hdr = QLabel("Model")
        hdr.setProperty("class", "headline-sm")
        lay.addWidget(hdr)

        row = QHBoxLayout()
        row.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        for m in SUPPORTED_MODELS:
            self.model_combo.addItem(m, m)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        row.addWidget(self.model_combo)
        row.addSpacing(SPACING_MD)
        self.model_status = QLabel("")
        self.model_status.setProperty("class", "label-sm")
        row.addWidget(self.model_status)
        row.addStretch()
        self.download_btn = QPushButton("Download Model")
        self.download_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_btn.clicked.connect(self._on_download)
        row.addWidget(self.download_btn)
        lay.addLayout(row)

        self.download_note = QLabel("")
        self.download_note.setProperty("class", "body-sm")
        self.download_note.setWordWrap(True)
        self.download_note.setVisible(False)
        lay.addWidget(self.download_note)
        return card

    def _build_advanced_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        self.adv_toggle = QCheckBox("Advanced settings")
        self.adv_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.adv_toggle.toggled.connect(self._on_advanced_toggled)
        lay.addWidget(self.adv_toggle)

        self.adv_body = QWidget()
        ab = QVBoxLayout(self.adv_body)
        ab.setContentsMargins(SPACING_XL, 0, 0, 0)
        ab.setSpacing(SPACING_XS)

        # Device + compute type
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Device:"))
        self.device_combo = QComboBox()
        self.device_combo.addItems(["cuda", "cpu"])
        self.device_combo.currentTextChanged.connect(self._on_device_changed)
        r1.addWidget(self.device_combo)
        r1.addSpacing(SPACING_MD)
        r1.addWidget(QLabel("Compute type:"))
        self.compute_combo = QComboBox()
        r1.addWidget(self.compute_combo)
        r1.addStretch()
        ab.addLayout(r1)

        # Language (locked)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Language:"))
        self.lang_edit = _disabled_edit("en")
        r2.addWidget(self.lang_edit)
        r2.addStretch()
        ab.addLayout(r2)

        # Temperature + thresholds
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Temperature:"))
        self.temp_spin = _dbl_spin(0.0, 0.0, 2.0, 0.1)
        r3.addWidget(self.temp_spin)
        r3.addSpacing(SPACING_MD)
        r3.addWidget(QLabel("No-speech threshold:"))
        self.no_speech_spin = _dbl_spin(0.6, 0.0, 1.0, 0.05)
        r3.addWidget(self.no_speech_spin)
        r3.addSpacing(SPACING_MD)
        r3.addWidget(QLabel("Log prob threshold:"))
        self.logprob_spin = _dbl_spin(-1.0, -2.0, 2.0, 0.1)
        r3.addWidget(self.logprob_spin)
        r3.addStretch()
        ab.addLayout(r3)

        # VAD
        r4 = QHBoxLayout()
        self.vad_cb = QCheckBox("VAD filter")
        self.vad_cb.setChecked(True)
        self.vad_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        r4.addWidget(self.vad_cb)
        r4.addSpacing(SPACING_MD)
        r4.addWidget(QLabel("VAD threshold:"))
        self.vad_thresh_spin = _dbl_spin(0.5, 0.0, 1.0, 0.05)
        r4.addWidget(self.vad_thresh_spin)
        r4.addStretch()
        ab.addLayout(r4)

        r5 = QHBoxLayout()
        r5.addWidget(QLabel("Min speech (ms):"))
        self.vad_min_speech = _int_spin(250, 0, 5000, 10)
        r5.addWidget(self.vad_min_speech)
        r5.addSpacing(SPACING_MD)
        r5.addWidget(QLabel("Min silence (ms):"))
        self.vad_min_silence = _int_spin(500, 0, 5000, 10)
        r5.addWidget(self.vad_min_silence)
        r5.addSpacing(SPACING_MD)
        r5.addWidget(QLabel("Speech pad (ms):"))
        self.vad_speech_pad = _int_spin(200, 0, 1000, 10)
        r5.addWidget(self.vad_speech_pad)
        r5.addStretch()
        ab.addLayout(r5)

        self.cond_prev_cb = QCheckBox("Condition on previous text")
        self.cond_prev_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        ab.addWidget(self.cond_prev_cb)

        self.adv_body.setVisible(False)
        lay.addWidget(self.adv_body)
        return card

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

    # --- Lifecycle ---------------------------------------------------------
    def on_enter(self) -> None:
        """Called when navigating to this stage."""
        if not self._state.working_folder:
            return
        self._context_store = ContextStore(
            paths.context_dir(self._state.working_folder)
        )
        # detect hardware accel once (cheap, cached)
        self._hwaccel = detect_hardware_accel()
        # load settings (or defaults) and populate controls
        s = self._state.transcription_settings
        self._loading = True
        try:
            self.enable_cb.setChecked(s.enabled)
            if s.model in SUPPORTED_MODELS:
                self.model_combo.setCurrentText(s.model)
            self.device_combo.setCurrentText(s.device or self._hwaccel)
            self._refresh_compute_options(s.device or self._hwaccel, s.compute_type)
            self.temp_spin.setValue(s.temperature)
            self.no_speech_spin.setValue(s.no_speech_threshold)
            self.logprob_spin.setValue(s.log_prob_threshold)
            self.vad_cb.setChecked(s.vad_filter)
            self.vad_thresh_spin.setValue(s.vad_threshold)
            self.vad_min_speech.setValue(s.vad_min_speech_duration_ms)
            self.vad_min_silence.setValue(s.vad_min_silence_duration_ms)
            self.vad_speech_pad.setValue(s.vad_speech_pad_ms)
            self.cond_prev_cb.setChecked(s.condition_on_previous_text)
        finally:
            self._loading = False

        self._populate_videos()
        self._refresh_model_status()
        self._update_button_state()

    def _populate_videos(self) -> None:
        self.model.clear()
        sel_paths = set(self._state.transcription_settings.selected_video_paths)
        for v in self._state.selected_videos:
            item = QStandardItem()
            item.setData(v.path, Qt.ItemDataRole.UserRole + 1)
            item.setData({"title": v.name, "subtitle": _dur_label(v.duration)},
                         Qt.ItemDataRole.UserRole)
            is_sel = v.path in sel_paths
            item.setData(Qt.CheckState.Checked if is_sel else Qt.CheckState.Unchecked,
                         Qt.ItemDataRole.CheckStateRole)
            pix = QPixmap()
            if v.thumbnail_path and Path(v.thumbnail_path).exists():
                pix = QPixmap(v.thumbnail_path)
            item.setData(pix, Qt.ItemDataRole.DecorationRole)
            item.setEditable(False)
            self.model.appendRow(item)
        self._update_count()

    # --- Selection ---------------------------------------------------------
    def _on_item_clicked(self, idx) -> None:
        current = idx.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        new_state = Qt.CheckState.Unchecked if current else Qt.CheckState.Checked
        self.model.setData(idx, new_state, Qt.ItemDataRole.CheckStateRole)
        self._save_selected_paths()
        self._update_count()
        self._update_button_state()

    def _on_context_menu(self, pos) -> None:
        """Right-click on a thumbnail -> preview / open / copy path."""
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        path = idx.data(Qt.ItemDataRole.UserRole + 1)
        if not path:
            return
        name = (idx.data(Qt.ItemDataRole.UserRole) or {}).get("title", "")
        show_video_context_menu(path, name,
                                self.view.viewport().mapToGlobal(pos), self)

    def _on_select_all(self) -> None:
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        self._save_selected_paths()
        self._update_count()
        self._update_button_state()

    def _on_select_none(self) -> None:
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self._save_selected_paths()
        self._update_count()
        self._update_button_state()

    def _save_selected_paths(self) -> None:
        selected: list[str] = []
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            if idx.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked:
                selected.append(idx.data(Qt.ItemDataRole.UserRole + 1))
        s = self._state.transcription_settings
        s.selected_video_paths = selected
        self._state.set_transcription_settings(s)

    def _update_count(self) -> None:
        sel = 0
        total = self.model.rowCount()
        for row in range(total):
            if self.model.item(row).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked:
                sel += 1
        self.count_label.setText(f"{sel} of {total} selected")

    # --- Enable / model / advanced ----------------------------------------
    def _on_enable_toggled(self, checked: bool) -> None:
        self._persist_settings()
        self._update_button_state()

    def _on_model_changed(self) -> None:
        self._persist_settings()
        self._refresh_model_status()
        self._update_button_state()

    def _on_device_changed(self, device: str) -> None:
        self._refresh_compute_options(device, default_compute_type_for_device(device))
        self._persist_settings()

    def _refresh_compute_options(self, device: str, prefer: str) -> None:
        self.compute_combo.blockSignals(True)
        self.compute_combo.clear()
        self.compute_combo.addItems(compute_type_options_for_device(device))
        if prefer in compute_type_options_for_device(device):
            self.compute_combo.setCurrentText(prefer)
        self.compute_combo.blockSignals(False)

    def _on_advanced_toggled(self, checked: bool) -> None:
        self.adv_body.setVisible(checked)

    def _refresh_model_status(self) -> None:
        model = self.model_combo.currentData() or self.model_combo.currentText()
        present = is_model_present(model, self._state.models_dir)
        if present:
            self.model_status.setText("✓ downloaded")
            self.model_status.setStyleSheet(
                f"color: {COLOR_SUCCESS}; font-weight: 600;")
            self.download_btn.setEnabled(False)
            self.download_btn.setText("Downloaded")
        else:
            self.model_status.setText("⚠ not downloaded")
            self.model_status.setStyleSheet(
                f"color: {COLOR_WARNING}; font-weight: 600;")
            self.download_btn.setEnabled(
                self.enable_cb.isChecked() and not self._is_busy())
            self.download_btn.setText("Download Model")
        self._update_button_state()

    # --- Settings persistence ---------------------------------------------
    def _persist_settings(self) -> None:
        if getattr(self, "_loading", False):
            return
        s = TranscriptionSettings(
            enabled=self.enable_cb.isChecked(),
            model=self.model_combo.currentData() or self.model_combo.currentText(),
            device=self.device_combo.currentText(),
            compute_type=self.compute_combo.currentText(),
            language="en",
            temperature=self.temp_spin.value(),
            condition_on_previous_text=self.cond_prev_cb.isChecked(),
            no_speech_threshold=self.no_speech_spin.value(),
            log_prob_threshold=self.logprob_spin.value(),
            vad_filter=self.vad_cb.isChecked(),
            vad_threshold=self.vad_thresh_spin.value(),
            vad_min_speech_duration_ms=self.vad_min_speech.value(),
            vad_min_silence_duration_ms=self.vad_min_silence.value(),
            vad_speech_pad_ms=self.vad_speech_pad.value(),
            selected_video_paths=self._state.transcription_settings.selected_video_paths,
        )
        self._state.set_transcription_settings(s)

    # --- Download ----------------------------------------------------------
    def _on_download(self) -> None:
        model = self.model_combo.currentData() or self.model_combo.currentText()
        if self._download_worker and self._download_worker.isRunning():
            return
        self.download_btn.setEnabled(False)
        self.download_btn.setText("Downloading...")
        self.download_note.setText(
            "Downloading model. This may take a few minutes on slow connections."
        )
        self.download_note.setVisible(True)
        self._download_worker = ModelDownloadWorker(
            model, self._state.models_dir, self,
        )
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.finished_download.connect(self._on_download_done)
        self._download_worker.start()

    def _on_download_progress(self, msg: str) -> None:
        self.download_note.setText(msg)

    def _on_download_done(self, success: bool) -> None:
        self._refresh_model_status()
        if success:
            self.download_note.setText(
                "Download complete. The model is ready to use."
            )
        else:
            self.download_note.setText(
                "Download failed. Check your connection and try again."
            )
        self._update_button_state()

    # --- Transcribe --------------------------------------------------------
    def _is_busy(self) -> bool:
        return ((self._transcribe_worker and self._transcribe_worker.isRunning())
                or (self._download_worker and self._download_worker.isRunning()))

    def _update_button_state(self) -> None:
        enabled = self.enable_cb.isChecked()
        any_selected = any(
            self.model.item(row).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
            for row in range(self.model.rowCount())
        )
        model = self.model_combo.currentData() or self.model_combo.currentText()
        model_ok = is_model_present(model, self._state.models_dir)
        busy = self._is_busy()
        self.transcribe_btn.setEnabled(
            enabled and any_selected and model_ok and not busy
        )
        # video grid + advanced controls are disabled when transcription is off
        self.view.setEnabled(enabled)
        self.select_all_btn.setEnabled(enabled)
        self.select_none_btn.setEnabled(enabled)
        self.model_combo.setEnabled(enabled and not busy)
        self.download_btn.setEnabled(
            enabled and not model_ok and not busy
        )
        self.adv_toggle.setEnabled(enabled and not busy)
        self.skip_btn.setEnabled(not busy)

    def _on_transcribe(self) -> None:
        if self._transcribe_worker and self._transcribe_worker.isRunning():
            return
        self._persist_settings()
        # gather selected videos
        sel_paths = set()
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            if idx.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked:
                sel_paths.add(idx.data(Qt.ItemDataRole.UserRole + 1))
        videos = [v for v in self._state.selected_videos if v.path in sel_paths]
        if not videos:
            return
        self._progress_block.setVisible(True)
        self.progress_bar.setRange(0, len(videos))
        self.progress_bar.setValue(0)
        self.log_box.clear()
        self.transcribe_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.back_btn.setEnabled(False)
        s = self._state.transcription_settings
        self._transcribe_worker = TranscriptionWorker(
            videos, s, self._state.models_dir,
            paths.transcription_dir(self._state.working_folder),
            self._context_store, self,
        )
        self._transcribe_worker.progress.connect(self._on_progress)
        self._transcribe_worker.log.connect(self._on_log)
        self._transcribe_worker.video_finished.connect(self._on_video_finished)
        self._transcribe_worker.finished_all.connect(self._on_finished)
        self._transcribe_worker.start()

    def _on_progress(self, done, total, msg) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self.progress_label.setText(msg)

    def _on_log(self, msg: str) -> None:
        self.log_box.appendPlainText(msg)

    def _on_video_finished(self, video, result) -> None:
        pass

    def _on_finished(self, any_failed: bool) -> None:
        self.transcribe_btn.setEnabled(True)
        self.skip_btn.setEnabled(True)
        self.back_btn.setEnabled(True)
        self._refresh_model_status()
        if not any_failed:
            self.progress_label.setText("Transcription complete.")
            # auto-advance to Frame Extraction (stage 4)
            self._state.set_stage(4)
        else:
            self.progress_label.setText("Completed with errors.")

    # --- Navigation --------------------------------------------------------
    def _on_skip(self) -> None:
        self._persist_settings()
        self._state.set_stage(4)  # Frame Extraction


# --- Helpers ----------------------------------------------------------------

def _dur_label(d: float) -> str:
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


def _disabled_edit(text: str):
    from PyQt6.QtWidgets import QLineEdit
    e = QLineEdit(text)
    e.setEnabled(False)
    return e


def _dbl_spin(value, minimum, maximum, step):
    sb = QDoubleSpinBox()
    sb.setRange(minimum, maximum)
    sb.setSingleStep(step)
    sb.setValue(value)
    sb.setDecimals(2)
    return sb


def _int_spin(value, minimum, maximum, step):
    sb = QSpinBox()
    sb.setRange(minimum, maximum)
    sb.setSingleStep(step)
    sb.setValue(value)
    return sb