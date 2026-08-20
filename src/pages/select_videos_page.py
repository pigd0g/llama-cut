from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..state import VIDEO_EXTENSIONS, Video
from ..theme import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
)
from ..workers.thumbnail_worker import ThumbnailWorker
from .widgets import ThumbDelegate


def list_videos(folder: str) -> list[Video]:
    out: list[Video] = []
    if not folder:
        return out
    p = Path(folder)
    if not p.is_dir():
        return out
    for entry in sorted(p.iterdir()):
        if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
            out.append(Video.from_path(entry))
    return out


class SelectVideosPage(QWidget):
    """Stage 1 — list videos, show thumbnails, allow selection."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._thumb_worker: ThumbnailWorker | None = None
        self._build()
        self._connect()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        # Header row
        header = QHBoxLayout()
        title = QLabel("Select Videos")
        title.setProperty("class", "headline-md")
        header.addWidget(title)
        header.addStretch()
        self.count_label = QLabel("0 of 0 selected")
        self.count_label.setProperty("class", "label-md")
        header.addWidget(self.count_label)
        root.addLayout(header)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(SPACING_SM)
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_btn.clicked.connect(self._on_select_all)
        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.deselect_all_btn.clicked.connect(self._on_deselect_all)
        toolbar.addWidget(self.select_all_btn)
        toolbar.addWidget(self.deselect_all_btn)
        toolbar.addStretch()
        self.scan_btn = QPushButton("Rescan Folder")
        self.scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_btn.clicked.connect(self.refresh)
        toolbar.addWidget(self.scan_btn)
        root.addLayout(toolbar)

        # List
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
        self.view.setProperty("class", "card")
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.clicked.connect(self._on_item_clicked)
        root.addWidget(self.view, 1)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch()
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setProperty("class", "primary")
        self.continue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_btn.setFixedHeight(36)
        self.continue_btn.setEnabled(False)
        self.continue_btn.clicked.connect(self._on_continue)
        footer.addWidget(self.continue_btn)
        root.addLayout(footer)

    def _connect(self) -> None:
        self._state.selection_changed.connect(self._update_count)

    # --- Lifecycle ----------------------------------------------------------
    def refresh(self) -> None:
        """Rescan the working folder and populate the list."""
        videos = list_videos(self._state.working_folder)
        # preserve existing selection + probed info if same path
        prev = {v.path: v for v in self._state.videos}
        for v in videos:
            if v.path in prev:
                old = prev[v.path]
                v.selected = old.selected
                v.probed = old.probed
                v.duration = old.duration
                v.width = old.width
                v.height = old.height
                v.codec = old.codec
                v.fps = old.fps
        self._state.set_videos(videos)
        self._populate()
        self._start_thumbnails()

    def _populate(self) -> None:
        self.model.clear()
        for v in self._state.videos:
            item = QStandardItem()
            item.setData(v.path, Qt.ItemDataRole.UserRole + 1)  # path stash
            item.setData({"title": v.name, "subtitle": _size_label(v.size_bytes)},
                         Qt.ItemDataRole.UserRole)
            item.setData(Qt.CheckState.Checked if v.selected else Qt.CheckState.Unchecked,
                         Qt.ItemDataRole.CheckStateRole)
            pix = QPixmap()
            if v.thumbnail_path and Path(v.thumbnail_path).exists():
                pix = QPixmap(v.thumbnail_path)
            item.setData(pix, Qt.ItemDataRole.DecorationRole)
            item.setEditable(False)
            self.model.appendRow(item)
        self._update_count()

    # --- Thumbnails ---------------------------------------------------------
    def _start_thumbnails(self) -> None:
        if self._thumb_worker and self._thumb_worker.isRunning():
            self._thumb_worker.cancel()
            self._thumb_worker.quit()
            self._thumb_worker.wait(2000)
        pending = [v.path for v in self._state.videos if not v.thumbnail_path]
        if not pending:
            self._refresh_thumbs_from_state()
            return
        thumbs_dir = paths.thumbs_dir(self._state.working_folder)
        self._thumb_worker = ThumbnailWorker(pending, thumbs_dir, self)
        self._thumb_worker.thumb_ready.connect(self._on_thumb_ready)
        self._thumb_worker.finished_all.connect(self._on_thumbs_done)
        self._thumb_worker.start()

    def _on_thumb_ready(self, video_path: str, thumb_path: str) -> None:
        for v in self._state.videos:
            if v.path == video_path:
                v.thumbnail_path = thumb_path
                break
        # update model row
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            if idx.data(Qt.ItemDataRole.UserRole + 1) == video_path:
                pix = QPixmap(thumb_path)
                self.model.setData(idx, pix, Qt.ItemDataRole.DecorationRole)
                break

    def _on_thumbs_done(self) -> None:
        self._state.persist()

    def _refresh_thumbs_from_state(self) -> None:
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            path = idx.data(Qt.ItemDataRole.UserRole + 1)
            for v in self._state.videos:
                if v.path == path and v.thumbnail_path:
                    if Path(v.thumbnail_path).exists():
                        self.model.setData(idx, QPixmap(v.thumbnail_path),
                                           Qt.ItemDataRole.DecorationRole)
                    break

    # --- Selection ----------------------------------------------------------
    def _on_item_clicked(self, idx) -> None:
        path = idx.data(Qt.ItemDataRole.UserRole + 1)
        current = idx.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        new_state = Qt.CheckState.Unchecked if current else Qt.CheckState.Checked
        self.model.setData(idx, new_state, Qt.ItemDataRole.CheckStateRole)
        for v in self._state.videos:
            if v.path == path:
                v.selected = not current
                break
        self._state.selection_changed.emit()
        self._state.persist()

    def _on_select_all(self) -> None:
        self._state.select_all()
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)

    def _on_deselect_all(self) -> None:
        self._state.deselect_all()
        for row in range(self.model.rowCount()):
            self.model.setData(self.model.index(row, 0),
                               Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)

    def _update_count(self) -> None:
        sel = sum(1 for v in self._state.videos if v.selected)
        total = len(self._state.videos)
        self.count_label.setText(f"{sel} of {total} selected")
        self.continue_btn.setEnabled(sel > 0)

    def _on_continue(self) -> None:
        self._state.set_stage(2)


def _size_label(n: int) -> str:
    if n <= 0:
        return ""
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if i > 0 else f"{n} B"