from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..context import ContextStore, ContextType
from ..frame_analysis import FrameAnalysisSettings, is_config_valid
from ..state import Frame
from ..theme import SPACING_LG, SPACING_MD, SPACING_SM
from ..workers.frame_analysis_worker import FrameAnalysisWorker
from .widgets import ThumbDelegate


class SelectFramesPage(QWidget):
    """Stage 5 — show all extracted frames, filter by video, select,
    and analyse each selected frame with an Ollama vision model."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._filter_video: str | None = None
        self._context_store: ContextStore | None = None
        self._worker: FrameAnalysisWorker | None = None
        self._loading = False
        self._build()
        self._connect()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        header = QHBoxLayout()
        title = QLabel("Select Frames")
        title.setProperty("class", "headline-md")
        header.addWidget(title)
        header.addStretch()
        self.count_label = QLabel("0 of 0 selected")
        self.count_label.setProperty("class", "label-md")
        header.addWidget(self.count_label)
        root.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(SPACING_SM)
        toolbar.addWidget(QLabel("Filter by video:"))
        self.video_filter = QComboBox()
        self.video_filter.setMinimumWidth(240)
        self.video_filter.currentIndexChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self.video_filter)
        toolbar.addSpacing(SPACING_MD)
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_btn.clicked.connect(self._on_select_all)
        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_none_btn.clicked.connect(self._on_select_none)
        toolbar.addWidget(self.select_all_btn)
        toolbar.addWidget(self.select_none_btn)
        toolbar.addStretch()
        root.addLayout(toolbar)

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
        root.addWidget(self.view, 1)

        root.addWidget(self._build_progress_block())

        footer = QHBoxLayout()
        footer.addWidget(QLabel("Concurrency:"))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 8)
        self.concurrency_spin.setFixedWidth(72)
        self.concurrency_spin.setToolTip(
            "Number of concurrent Ollama API calls."
        )
        self.concurrency_spin.valueChanged.connect(self._on_concurrency_changed)
        footer.addWidget(self.concurrency_spin)
        footer.addStretch()
        self.back_btn = QPushButton("Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(lambda: self._state.set_stage(4))
        footer.addWidget(self.back_btn)
        self.analyse_btn = QPushButton("Analyse")
        self.analyse_btn.setProperty("class", "primary")
        self.analyse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.analyse_btn.clicked.connect(self._on_analyse)
        footer.addWidget(self.analyse_btn)
        root.addLayout(footer)

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

    def _connect(self) -> None:
        self._state.frames_changed.connect(self._on_frames_changed)

    # --- Lifecycle ----------------------------------------------------------
    def on_enter(self) -> None:
        self._context_store = ContextStore(
            Path(self._state.working_folder) / "context"
        ) if self._state.working_folder else None
        self._populate_video_filter()
        self._populate()
        # load concurrency from state without triggering the changed signal
        self._loading = True
        try:
            self.concurrency_spin.setValue(
                self._state.frame_analysis_settings.concurrency
            )
        finally:
            self._loading = False
        self._update_button_state()

    def _on_frames_changed(self) -> None:
        self._populate_video_filter()
        self._populate()

    def _populate_video_filter(self) -> None:
        # build from frames' unique video paths + names
        videos = {}
        for f in self._state.frames:
            videos[f.video_path] = f.video_stem
        current = self.video_filter.currentData()
        self.video_filter.blockSignals(True)
        self.video_filter.clear()
        self.video_filter.addItem("All Videos", None)
        for path, stem in videos.items():
            self.video_filter.addItem(stem, path)
        # restore selection
        if current is not None:
            idx = self.video_filter.findData(current)
            if idx >= 0:
                self.video_filter.setCurrentIndex(idx)
        self.video_filter.blockSignals(False)
        self._filter_video = self.video_filter.currentData()

    def _populate(self) -> None:
        self.model.clear()
        frames = self._visible_frames()
        for f in frames:
            item = QStandardItem()
            item.setData(f.path, Qt.ItemDataRole.UserRole + 1)
            item.setData({
                "title": f.filename,
                "subtitle": f"{_pts_label(f.pts_time)}  ·  {f.video_stem}",
            }, Qt.ItemDataRole.UserRole)
            item.setData(Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
            pix = QPixmap()
            if Path(f.path).exists():
                pix = QPixmap(f.path)
            item.setData(pix, Qt.ItemDataRole.DecorationRole)
            item.setEditable(False)
            self.model.appendRow(item)
        self._update_count()

    def _visible_frames(self) -> list[Frame]:
        if self._filter_video is None:
            return list(self._state.frames)
        return [f for f in self._state.frames if f.video_path == self._filter_video]

    def _on_filter_changed(self) -> None:
        self._filter_video = self.video_filter.currentData()
        self._populate()

    def _on_item_clicked(self, idx) -> None:
        current = idx.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        new_state = Qt.CheckState.Unchecked if current else Qt.CheckState.Checked
        self.model.setData(idx, new_state, Qt.ItemDataRole.CheckStateRole)
        self._update_count()
        self._update_button_state()

    def _on_select_all(self) -> None:
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        self._update_count()
        self._update_button_state()

    def _on_select_none(self) -> None:
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self._update_count()
        self._update_button_state()

    def _update_count(self) -> None:
        sel = sum(
            1 for row in range(self.model.rowCount())
            if self.model.item(row).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        )
        total = self.model.rowCount()
        self.count_label.setText(f"{sel} of {total} selected")

    # --- Concurrency setting ------------------------------------------------
    def _on_concurrency_changed(self, value: int) -> None:
        if self._loading:
            return
        self._state.set_frame_analysis_settings(FrameAnalysisSettings(concurrency=value))

    # --- Analyse ------------------------------------------------------------
    def _selected_frames(self) -> list[Frame]:
        """Return the Frame objects the user has checked, in chronological order."""
        if self._filter_video is not None:
            # When a filter is active, the visible set is the filter subset.
            frame_by_path = {f.path: f for f in self._state.frames
                             if f.video_path == self._filter_video}
        else:
            frame_by_path = {f.path: f for f in self._state.frames}
        paths: list[str] = []
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            if idx.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked:
                p = idx.data(Qt.ItemDataRole.UserRole + 1)
                if p in frame_by_path:
                    paths.append(p)
        frames = [frame_by_path[p] for p in paths]
        # Chronological order: by video_path first-seen, then pts_time.
        return sorted(frames, key=lambda f: (f.video_path, f.pts_time))

    def _is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _update_button_state(self) -> None:
        sel = self._selected_frames()
        busy = self._is_busy()
        self.analyse_btn.setEnabled(bool(sel) and not busy)
        self.back_btn.setEnabled(not busy)
        self.concurrency_spin.setEnabled(not busy)
        self.select_all_btn.setEnabled(not busy)
        self.select_none_btn.setEnabled(not busy)
        self.video_filter.setEnabled(not busy)

    def _on_analyse(self) -> None:
        if self._is_busy():
            return
        ok, msg = is_config_valid()
        if not ok:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Configuration missing", msg)
            return
        frames = self._selected_frames()
        if not frames:
            return
        if self._context_store is None:
            return

        # Load project context + per-video video context once for the run.
        project_doc = self._context_store.get(None, ContextType.PROJECT)
        project_ctx = project_doc.content if project_doc else ""
        video_contexts: dict[str, str] = {}
        for stem in {f.video_stem for f in frames}:
            vdoc = self._context_store.get(stem, ContextType.VIDEO)
            video_contexts[stem] = vdoc.content if vdoc else ""

        self._progress_block.setVisible(True)
        self.progress_bar.setRange(0, len(frames))
        self.progress_bar.setValue(0)
        self.log_box.clear()
        self.analyse_btn.setEnabled(False)
        self.back_btn.setEnabled(False)
        self.concurrency_spin.setEnabled(False)
        s = self._state.frame_analysis_settings
        self._worker = FrameAnalysisWorker(
            frames, s, project_ctx, video_contexts, self._context_store, self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._on_log)
        self._worker.frame_finished.connect(self._on_frame_finished)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, done, total, msg) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self.progress_label.setText(msg)

    def _on_log(self, msg: str) -> None:
        self.log_box.appendPlainText(msg)

    def _on_frame_finished(self, frame, section_text) -> None:
        pass  # progress handled by the progress signal

    def _on_finished(self, any_failed: bool) -> None:
        self._update_button_state()
        if not any_failed:
            self.progress_label.setText("Frame analysis complete.")
        else:
            self.progress_label.setText("Completed with errors.")


def _pts_label(pts: float) -> str:
    h = int(pts // 3600)
    m = int((pts % 3600) // 60)
    s = int(pts % 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"