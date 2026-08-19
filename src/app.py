from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .state import PipelineState
from .theme import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
)
from .pages.context_page import ContextPage
from .pages.context_review_page import ContextReviewPage
from .pages.frame_generation_page import FrameGenerationPage
from .pages.select_frames_page import SelectFramesPage
from .pages.select_videos_page import SelectVideosPage
from .pages.transcription_page import TranscriptionPage
from .pages.welcome_page import WelcomePage


# Stage indices: 0=Welcome, 1=Select Videos, 2=Context, 3=Transcription,
# 4=Frame Generation, 5=Select Frames, 6=Context Review
NAV_ICONS = {
    0: "\ue8cc",  # folder
    1: "\ue8cc",  # folder
    2: "\ue873",  # description / article
    3: "\ue31a",  # mic
    4: "\ue02a",  # video
    5: "\ue413",  # photo_library
    6: "\ue873",  # description / article (context review)
}
NAV_LABELS = {
    0: "Welcome",
    1: "1 · Select Videos",
    2: "2 · Context",
    3: "3 · Transcription",
    4: "4 · Frame Generation",
    5: "5 · Select Frames",
    6: "6 · Context Review",
}


class AppShell(QMainWindow):
    def __init__(self, state: PipelineState | None = None):
        super().__init__()
        self._state = state or PipelineState()
        self.setWindowTitle("llama-cut")
        self.setMinimumSize(1100, 720)
        self.resize(1600, 860)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = self._build_sidebar()
        root.addWidget(self.sidebar)

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(0)
        self.stack = QStackedWidget()
        self.welcome_page = WelcomePage(self._state)
        self.stage1 = SelectVideosPage(self._state)
        self.context_page = ContextPage(self._state)
        self.transcription_page = TranscriptionPage(self._state)
        self.stage4 = FrameGenerationPage(self._state)
        self.stage5 = SelectFramesPage(self._state)
        self.context_review_page = ContextReviewPage(self._state)
        self.stack.addWidget(self.welcome_page)        # index 0
        self.stack.addWidget(self.stage1)              # index 1
        self.stack.addWidget(self.context_page)        # index 2
        self.stack.addWidget(self.transcription_page)   # index 3
        self.stack.addWidget(self.stage4)               # index 4
        self.stack.addWidget(self.stage5)               # index 5
        self.stack.addWidget(self.context_review_page)  # index 6
        right_col.addWidget(self.stack, 1)
        right_widget = QWidget()
        right_widget.setLayout(right_col)
        root.addWidget(right_widget, 1)

        self._nav_buttons: list[QToolButton] = []
        self._build_nav_items()

        self._connect_signals()
        self._update_title()
        self._update_nav_enabled()
        self._on_stage_changed(self._state.stage)

    # --- Sidebar ------------------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(240)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, SPACING_LG, 0, SPACING_LG)
        lay.setSpacing(0)

        brand = QWidget()
        bl = QVBoxLayout(brand)
        bl.setContentsMargins(SPACING_MD, 0, SPACING_MD, 0)
        bl.setSpacing(SPACING_XS)
        title = QLabel("llama-cut")
        title.setProperty("class", "headline-md")
        subtitle = QLabel("VIDEO PIPELINE")
        subtitle.setProperty("class", "label-md")
        bl.addWidget(title)
        bl.addWidget(subtitle)
        lay.addWidget(brand)
        lay.addSpacing(SPACING_XL)

        self._nav_container = QWidget()
        self._nav_layout = QVBoxLayout(self._nav_container)
        self._nav_layout.setContentsMargins(SPACING_SM, 0, SPACING_SM, 0)
        self._nav_layout.setSpacing(SPACING_XS)
        lay.addWidget(self._nav_container)
        lay.addStretch()

        # working folder footer
        self.folder_label = QLabel("No folder selected")
        self.folder_label.setProperty("class", "label-sm")
        self.folder_label.setWordWrap(True)
        self.folder_label.setContentsMargins(SPACING_MD, 0, SPACING_MD, SPACING_MD)
        lay.addWidget(self.folder_label)
        return bar

    def _build_nav_items(self) -> None:
        for stage in (0, 1, 2, 3, 4, 5, 6):
            btn = QToolButton()
            btn.setObjectName("sidebarNavItem")
            btn.setProperty("active", False)
            btn.setText(NAV_LABELS[stage])
            btn.setFont(_icon_font(14))
            # use a left icon via setText approach — keep simple text+emoji style
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            btn.setFixedHeight(40)
            btn.setSizePolicy(btn.sizePolicy().Policy.Expanding,
                              btn.sizePolicy().Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked, s=stage: self._on_nav_click(s))
            self._nav_layout.addWidget(btn)
            self._nav_buttons.append(btn)
        self._set_active_nav(self._state.stage)

    def _set_active_nav(self, stage: int) -> None:
        for i, btn in enumerate(self._nav_buttons):
            is_active = i == stage
            btn.setProperty("active", is_active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _update_nav_enabled(self) -> None:
        folder_set = bool(self._state.working_folder)
        for i, btn in enumerate(self._nav_buttons):
            # Welcome (0) always enabled; stages need a folder
            btn.setEnabled(i == 0 or folder_set)

    # --- Signals ------------------------------------------------------------
    def _connect_signals(self) -> None:
        self._state.working_folder_changed.connect(self._on_folder_changed)
        self._state.stage_changed.connect(self._on_stage_changed)

    def _on_folder_changed(self, folder: str) -> None:
        self._update_title()
        self._update_nav_enabled()
        if folder:
            self.folder_label.setText(_short_path(folder))
            self._state.load()
            # refresh stage 1
            self.stage1.refresh()
            # jump to stage 1 if we were on welcome
            if self._state.stage == 0:
                self._state.set_stage(1)
        else:
            self.folder_label.setText("No folder selected")

    def _on_stage_changed(self, stage: int) -> None:
        if stage < 0 or stage >= self.stack.count():
            stage = 0
        self.stack.setCurrentIndex(stage)
        self._set_active_nav(stage)
        self._update_nav_enabled()
        # notify pages
        if stage == 1:
            self.stage1.refresh()
        elif stage == 2:
            self.context_page.on_enter()
        elif stage == 3:
            self.transcription_page.on_enter()
        elif stage == 4:
            self.stage4.on_enter()
        elif stage == 5:
            self.stage5.on_enter()
        elif stage == 6:
            self.context_review_page.on_enter()

    def _on_nav_click(self, stage: int) -> None:
        if stage == 0:
            # Welcome — go back to folder-less state? No — keep folder, go to stage 1
            if self._state.working_folder:
                self._state.set_stage(1)
            else:
                self._state.set_stage(0)
            return
        if not self._state.working_folder:
            return
        self._state.set_stage(stage)

    # --- Title --------------------------------------------------------------
    def _update_title(self) -> None:
        folder = self._state.working_folder
        if folder:
            name = Path(folder).name
            self.setWindowTitle(f"llama-cut — {name}  ({folder})")
        else:
            self.setWindowTitle("llama-cut")


def _icon_font(size: int):
    from PyQt6.QtGui import QFont
    f = QFont("Segoe UI", 10)
    f.setPixelSize(size)
    return f


def _short_path(p: str, max_len: int = 28) -> str:
    if len(p) <= max_len:
        return p
    return "…" + p[-(max_len - 1):]