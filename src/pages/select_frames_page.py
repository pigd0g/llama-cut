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
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..state import Frame
from ..theme import SPACING_LG, SPACING_MD, SPACING_SM
from .widgets import ThumbDelegate


class SelectFramesPage(QWidget):
    """Stage 3 — show all extracted frames, filter by video, select."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._filter_video: str | None = None
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

        footer = QHBoxLayout()
        footer.addStretch()
        self.back_btn = QPushButton("Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(lambda: self._state.set_stage(4))
        footer.addWidget(self.back_btn)
        root.addLayout(footer)

    def _connect(self) -> None:
        self._state.frames_changed.connect(self._on_frames_changed)

    # --- Lifecycle ----------------------------------------------------------
    def on_enter(self) -> None:
        self._populate_video_filter()
        self._populate()

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

    def _on_select_all(self) -> None:
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        self._update_count()

    def _on_select_none(self) -> None:
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self._update_count()

    def _update_count(self) -> None:
        sel = sum(
            1 for row in range(self.model.rowCount())
            if self.model.item(row).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        )
        total = self.model.rowCount()
        self.count_label.setText(f"{sel} of {total} selected")


def _pts_label(pts: float) -> str:
    h = int(pts // 3600)
    m = int((pts % 3600) // 60)
    s = int(pts % 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"