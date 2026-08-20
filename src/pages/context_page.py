from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import ContextStore, ContextType
from .. import paths
from ..state import Video
from ..theme import (
    COLOR_BORDER,
    COLOR_ON_SURFACE,
    COLOR_ON_SURFACE_VARIANT,
    COLOR_PRIMARY,
    COLOR_SURFACE_CONTAINER,
    RADIUS_DEFAULT,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XS,
)
from .markdown_editor import MarkdownEditor


THUMB_W = 64
THUMB_H = 36


class _ScopeRow(QPushButton):
    """A selectable row in the scope list: thumbnail + name + duration."""

    def __init__(self, label: str, subtitle: str, thumb_path: str,
                 is_project: bool, video: Video | None = None):
        super().__init__()
        self.setObjectName("scopeRow")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(56)
        self.setProperty("active", False)
        self.video = video  # None for Project row

        lay = QHBoxLayout(self)
        lay.setContentsMargins(SPACING_SM, SPACING_SM, SPACING_SM, SPACING_SM)
        lay.setSpacing(SPACING_SM)

        # Thumbnail (or icon placeholder for Project)
        thumb = QLabel()
        thumb.setFixedSize(THUMB_W, THUMB_H)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if is_project:
            thumb.setText("\ue873")  # description / article material symbol
            thumb.setStyleSheet(
                f"font-family: 'Material Symbols Outlined'; font-size: 24px; "
                f"color: {COLOR_PRIMARY}; background-color: {COLOR_SURFACE_CONTAINER}; "
                f"border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_DEFAULT}px;"
            )
        else:
            pix = QPixmap()
            if thumb_path and Path(thumb_path).exists():
                pix = QPixmap(thumb_path)
            if pix.isNull():
                thumb.setText("—")
                thumb.setStyleSheet(
                    f"color: {COLOR_ON_SURFACE_VARIANT}; background-color: "
                    f"{COLOR_SURFACE_CONTAINER}; border: 1px solid "
                    f"{COLOR_BORDER}; border-radius: {RADIUS_DEFAULT}px;"
                )
            else:
                scaled = pix.scaled(
                    THUMB_W, THUMB_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                thumb.setPixmap(scaled)
        lay.addWidget(thumb)

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        name = QLabel(label)
        name.setStyleSheet(f"font-weight: 600; color: {COLOR_ON_SURFACE};")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        sub = QLabel(subtitle)
        sub.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT}; font-size: 11px;")
        sub.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text_col.addWidget(name)
        text_col.addWidget(sub)
        lay.addLayout(text_col, 1)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setChecked(active)


class ContextPage(QWidget):
    """Stage 2 — author/edit Project and Video Markdown context."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._store: ContextStore | None = None
        self._scope_rows: list[_ScopeRow] = []
        self._selected_video: Video | None = None  # None = Project
        self._project_editor: MarkdownEditor | None = None
        self._build()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        # Header
        header = QHBoxLayout()
        title = QLabel("Context")
        title.setProperty("class", "headline-md")
        header.addWidget(title)
        header.addStretch()
        root.addLayout(header)

        # Body: scope list (left) + editor pane (right)
        body = QHBoxLayout()
        body.setSpacing(SPACING_MD)

        # Left: scope list inside a card
        scope_card = QFrame()
        scope_card.setProperty("class", "card")
        scope_card.setFixedWidth(220)
        sl = QVBoxLayout(scope_card)
        sl.setContentsMargins(SPACING_SM, SPACING_MD, SPACING_SM, SPACING_MD)
        sl.setSpacing(SPACING_XS)
        scope_hdr = QLabel("Scope")
        scope_hdr.setProperty("class", "label-md")
        sl.addWidget(scope_hdr)
        sl.addSpacing(SPACING_XS)

        self.scope_scroll = QScrollArea()
        self.scope_scroll.setWidgetResizable(True)
        self.scope_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scope_scroll.setStyleSheet("background: transparent;")
        self.scope_container = QWidget()
        self.scope_layout = QVBoxLayout(self.scope_container)
        self.scope_layout.setContentsMargins(0, 0, 0, 0)
        self.scope_layout.setSpacing(SPACING_XS)
        self.scope_scroll.setWidget(self.scope_container)
        sl.addWidget(self.scope_scroll, 1)
        body.addWidget(scope_card)

        # Right: stacked editor pane (Project vs Video tabs)
        self.editor_stack = QStackedWidget()
        self.project_page = QWidget()
        self.project_page.setLayout(QVBoxLayout())
        self.project_page.layout().setContentsMargins(0, 0, 0, 0)
        self.project_page.layout().setSpacing(SPACING_SM)
        self.editor_stack.addWidget(self.project_page)  # index 0

        self.video_page = QWidget()
        vpl = QVBoxLayout(self.video_page)
        vpl.setContentsMargins(0, 0, 0, 0)
        self.video_tabs = QTabWidget()
        vpl.addWidget(self.video_tabs)
        self.editor_stack.addWidget(self.video_page)  # index 1
        body.addWidget(self.editor_stack, 1)

        root.addLayout(body, 1)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch()
        self.back_btn = QPushButton("Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self._on_back)
        footer.addWidget(self.back_btn)
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setProperty("class", "primary")
        self.continue_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_btn.clicked.connect(self._on_continue)
        footer.addWidget(self.continue_btn)
        root.addLayout(footer)

    # --- Lifecycle ---------------------------------------------------------
    def on_enter(self) -> None:
        """Called when navigating to this stage. Rebuilds scope list."""
        if not self._state.working_folder:
            return
        self._store = ContextStore(paths.context_dir(self._state.working_folder))
        self._rebuild_scope_list()
        self._ensure_project_editor()
        # restore last selection or default to Project
        sel_paths = {v.path for v in self._state.selected_videos}
        if self._selected_video is not None and self._selected_video.path not in sel_paths:
            self._selected_video = None
        self._select_scope(self._selected_video)

    def _rebuild_scope_list(self) -> None:
        # clear existing rows
        for row in list(self._scope_rows):
            self.scope_layout.removeWidget(row)
            row.deleteLater()
        self._scope_rows = []

        # Project row
        project_row = _ScopeRow("Project", "applies to all videos", "",
                                 is_project=True, video=None)
        project_row.clicked.connect(lambda _checked: self._select_scope(None))
        self.scope_layout.addWidget(project_row)
        self._scope_rows.append(project_row)

        # Per-video rows
        for v in self._state.selected_videos:
            subtitle = _duration_label(v.duration) if v.duration > 0 else v.name
            row = _ScopeRow(v.name, subtitle, v.thumbnail_path,
                            is_project=False, video=v)
            row.clicked.connect(lambda _checked, vid=v: self._select_scope(vid))
            self.scope_layout.addWidget(row)
            self._scope_rows.append(row)

        self.scope_layout.addStretch()

    def _ensure_project_editor(self) -> None:
        if self._project_editor is None:
            self._project_editor = MarkdownEditor(
                self._store, None, ContextType.PROJECT, self.project_page,
            )
            self.project_page.layout().addWidget(self._project_editor)
        else:
            self._project_editor.reload()

    # --- Scope selection ---------------------------------------------------
    def _select_scope(self, video: Video | None) -> None:
        self._selected_video = video
        if video is None:
            self.editor_stack.setCurrentIndex(0)
            self._ensure_project_editor()
        else:
            self._rebuild_video_tabs(video)
            self.editor_stack.setCurrentIndex(1)
        # highlight matching row
        for row in self._scope_rows:
            row.set_active(row.video is None and video is None
                           or (row.video is not None and video is not None
                               and row.video.path == video.path))

    def _rebuild_video_tabs(self, video: Video) -> None:
        self.video_tabs.clear()
        for ctype in (ContextType.VIDEO, ContextType.FRAME_ANALYSIS,
                      ContextType.TRANSCRIPTION):
            editor = MarkdownEditor(self._store, video.stem, ctype, self)
            index = self.video_tabs.addTab(editor, _slot_tab_label(ctype))
            # Disable the programmatic slots until they have been generated
            # (i.e. the .md file exists on disk). The Video Context tab is
            # always enabled.
            if ctype is not ContextType.VIDEO:
                exists = editor._doc is not None and editor._doc.exists()
                self.video_tabs.setTabEnabled(index, exists)
                if not exists:
                    self.video_tabs.setTabToolTip(
                        index, "Not yet generated — will be populated in a "
                                "later phase.",
                    )

    # --- Navigation --------------------------------------------------------
    def _on_back(self) -> None:
        self._save_all_open_editors()
        self._state.set_stage(1)

    def _on_continue(self) -> None:
        self._save_all_open_editors()
        self._state.set_stage(3)  # Frame Extraction

    def _save_all_open_editors(self) -> None:
        if self._project_editor is not None:
            self._project_editor.save_now()
        # video tabs are rebuilt per selection; save the current tab widgets
        for i in range(self.video_tabs.count()):
            w = self.video_tabs.widget(i)
            if isinstance(w, MarkdownEditor):
                w.save_now()


# --- Helpers ----------------------------------------------------------------

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


def _slot_tab_label(ctype: ContextType) -> str:
    return {
        ContextType.VIDEO: "Video Context",
        ContextType.FRAME_ANALYSIS: "Frame Analysis",
        ContextType.TRANSCRIPTION: "Transcription",
    }.get(ctype, ctype.value)