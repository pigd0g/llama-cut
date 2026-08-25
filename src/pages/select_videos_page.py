from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
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
from ..workers.probe_worker import ProbeWorker
from ..workers.thumbnail_worker import ThumbnailWorker
from .video_preview import show_video_context_menu
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
        self._probe_worker: ProbeWorker | None = None
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
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
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
        # Re-populate subtitles when probing completes so resolution/duration
        # (which are only known after ffprobe runs) are reflected in the cards.
        self._state.videos_probed.connect(self._populate)

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
                # Carry the thumbnail across rescans so a video whose card
                # already has one doesn't re-run ffmpeg (and so a stale
                # state entry that forgot it can pick it up again).
                v.thumbnail_path = old.thumbnail_path
        self._state.set_videos(videos)
        self._populate()
        self._start_thumbnails()
        # Probe any unprobed videos so resolution/duration show in the cards.
        self._start_probe([v for v in videos if not v.probed])

    # --- Probing -----------------------------------------------------------
    def _start_probe(self, videos: list[Video]) -> None:
        if not videos:
            return
        if self._probe_worker and self._probe_worker.isRunning():
            self._probe_worker.cancel()
            self._probe_worker.quit()
            self._probe_worker.wait(2000)
        self._probe_worker = ProbeWorker(videos, self)
        self._probe_worker.video_probed.connect(self._on_video_probed)
        self._probe_worker.finished_all.connect(self._on_probe_done)
        self._probe_worker.start()

    def _on_video_probed(self, video: Video) -> None:
        # Update in-memory state + the model row so the subtitle (resolution +
        # duration) fills in as each probe completes.
        self._state.update_video(video)
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            if idx.data(Qt.ItemDataRole.UserRole + 1) == video.path:
                self.model.setData(
                    idx,
                    {"title": video.name,
                     "subtitle": _meta_label(video.size_bytes,
                                              video.width, video.height,
                                              video.duration)},
                    Qt.ItemDataRole.UserRole,
                )
                break

    def _on_probe_done(self) -> None:
        self._state.mark_probed()

    def _populate(self) -> None:
        self.model.clear()
        for v in self._state.videos:
            item = QStandardItem()
            item.setData(v.path, Qt.ItemDataRole.UserRole + 1)  # path stash
            item.setData({"title": v.name,
                          "subtitle": _meta_label(v.size_bytes,
                                                   v.width, v.height,
                                                   v.duration)},
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
            # Kill the in-flight ffmpeg before starting a replacement: a
            # superseded worker that keeps running would race the new one
            # writing the same .jpg (that is what left the first video's
            # thumbnail blank while the rest populated).
            self._thumb_worker.kill()
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


def _meta_label(size_bytes: int, width: int, height: float, duration: float) -> str:
    """Build the subtitle line: size (Mb), resolution, duration (hh:mm:ss)."""
    parts: list[str] = []
    size = _size_label(size_bytes)
    if size:
        parts.append(size)
    if width and height:
        parts.append(f"{int(width)}x{int(height)}")
    if duration > 0:
        parts.append(_duration_hms(duration))
    return "  ·  ".join(parts)


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


def _duration_hms(seconds: float) -> str:
    """Render a duration in seconds as H:MM:SS (or M:SS for short videos)."""
    if seconds <= 0:
        return ""
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"