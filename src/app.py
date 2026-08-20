from __future__ import annotations

import shutil
from pathlib import Path

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import paths
from .icons import material_icon_pixmap
from .state import PipelineState
from .theme import (
    COLOR_DANGER,
    COLOR_ON_SURFACE,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
)
from .pages.context_page import ContextPage
from .pages.context_review_page import ContextReviewPage
from .pages.frame_generation_page import FrameGenerationPage
from .pages.result_page import ResultPage
from .pages.select_frames_page import SelectFramesPage
from .pages.select_videos_page import SelectVideosPage
from .pages.storyboard_page import StoryboardPage
from .pages.transcription_page import TranscriptionPage
from .pages.video_production_page import VideoProductionPage
from .pages.welcome_page import WelcomePage


# Stage indices: 0=Welcome, 1=Select Videos, 2=Context, 3=Transcription,
# 4=Frame Extraction, 5=Frame Analysis, 6=Context Review, 7=Storyboard,
# 8=Final Video, 9=Result
# Icons use Material Symbols ligature names (never raw codepoints) — rendered
# via the material_icon helper, per the Icons section in AGENTS.md.
NAV_ICONS = {
    0: "home",           # Welcome
    1: "video_library",  # Select Videos
    2: "description",    # Context
    3: "mic",            # Transcription
    4: "movie",          # Frame Extraction
    5: "photo_library",  # Frame Analysis
    6: "view_quilt",     # Context Review
    7: "dashboard",      # Storyboard
    8: "video_settings", # Final Video
    9: "play_circle",    # Result
}
NAV_LABELS = {
    0: "Welcome",
    1: "Select Videos",
    2: "Context",
    3: "Transcription",
    4: "Frame Extraction",
    5: "Frame Analysis",
    6: "Context Review",
    7: "Storyboard",
    8: "Final Video",
    9: "Result",
}

# Pixel gap between the nav icon and its label. Baked into the icon pixmap
# as a transparent right margin so QToolButton renders it 1:1.
ICON_TEXT_GAP = 8


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
        self.storyboard_page = StoryboardPage(self._state)
        self.video_production_page = VideoProductionPage(self._state)
        self.result_page = ResultPage(self._state)
        self.stack.addWidget(self.welcome_page)        # index 0
        self.stack.addWidget(self.stage1)              # index 1
        self.stack.addWidget(self.context_page)        # index 2
        self.stack.addWidget(self.transcription_page)  # index 3
        self.stack.addWidget(self.stage4)               # index 4
        self.stack.addWidget(self.stage5)               # index 5
        self.stack.addWidget(self.context_review_page)  # index 6
        self.stack.addWidget(self.storyboard_page)     # index 7
        self.stack.addWidget(self.video_production_page) # index 8
        self.stack.addWidget(self.result_page)          # index 9
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

        # Hero image: scaled to the sidebar width, below the brand block.
        img_path = Path(__file__).resolve().parent.parent / "assets" / "llama-edit-crew.png"
        if img_path.exists():
            hero = QLabel()
            hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pix = QPixmap(str(img_path))
            if not pix.isNull():
                # Sidebar is 240px; leave SPACING_MD on each side.
                target_w = 240 - 2 * SPACING_MD
                scaled = pix.scaledToWidth(
                    target_w,
                    Qt.TransformationMode.SmoothTransformation,
                )
                hero.setPixmap(scaled)
            lay.addWidget(hero)

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
        self.folder_label.setContentsMargins(SPACING_MD, 0, SPACING_MD, 0)
        lay.addWidget(self.folder_label)

        # Destructive cleanup: removes the .llama-cut folder + all subdirs.
        self.cleanup_btn = QPushButton("Clean Up Project")
        self.cleanup_btn.setObjectName("dangerButton")
        self.cleanup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cleanup_btn.setContentsMargins(SPACING_MD, 0, SPACING_MD, SPACING_MD)
        self.cleanup_btn.setEnabled(False)
        self.cleanup_btn.setStyleSheet(
            f"QPushButton {{ color: {COLOR_DANGER}; "
            f"background: transparent; border: none; "
            f"text-align: left; padding: {SPACING_XS}px {SPACING_MD}px; }}"
            f"QPushButton:hover {{ background-color: {COLOR_DANGER}22; }}"
            f"QPushButton:disabled {{ color: {COLOR_DANGER}66; }}"
        )
        self.cleanup_btn.clicked.connect(self._on_cleanup)
        lay.addWidget(self.cleanup_btn)
        lay.addSpacing(SPACING_MD)
        return bar

    def _build_nav_items(self) -> None:
        for stage in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9):
            btn = QToolButton()
            btn.setObjectName("sidebarNavItem")
            btn.setProperty("active", False)
            btn.setText(NAV_LABELS[stage])
            btn.setFont(_icon_font(14))
            # Material Symbols icon to the left of the label, with a small
            # transparent right margin baked into the pixmap so there is a
            # gap between the icon and the text.
            icon_pix = _padded_nav_icon(NAV_ICONS[stage], 22,
                                        ICON_TEXT_GAP, COLOR_ON_SURFACE)
            btn.setIcon(QIcon(icon_pix))
            btn.setIconSize(icon_pix.size())
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            btn.setFixedHeight(44)
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
        self.cleanup_btn.setEnabled(folder_set)

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
            # jump to stage 1 — always force it so the stack switches even
            # if load() restored the same stage value internally.
            self._state.set_stage(1, force=True)
        else:
            self.folder_label.setText("No folder selected")
            # No folder -> back to the Welcome / folder-picker screen.
            self._state.set_stage(0, force=True)

    def _on_stage_changed(self, stage: int) -> None:
        if stage < 0 or stage >= self.stack.count():
            stage = 0
        self.stack.setCurrentIndex(stage)
        self._set_active_nav(stage)
        self._update_nav_enabled()
        # notify pages
        if stage == 0:
            self.welcome_page.on_enter()
        elif stage == 1:
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
        elif stage == 7:
            self.storyboard_page.on_enter()
        elif stage == 8:
            self.video_production_page.on_enter()
        elif stage == 9:
            self.result_page.on_enter()

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

    def _on_cleanup(self) -> None:
        """Destructive: delete the .llama-cut folder + all subdirs.

        Prompts the user to confirm; on accept, removes the entire
        ``.llama-cut`` tree under the working folder, resets in-memory state
        (frames, settings, stage), and re-enters stage 1.
        """
        folder = self._state.working_folder
        if not folder:
            return
        app_root = paths.app_root(folder)
        if not app_root.exists():
            # Nothing to delete — still reset state so the UI is consistent.
            self._reset_after_cleanup(folder)
            return

        confirm = QMessageBox(
            QMessageBox.Icon.Warning,
            "Clean Up Project",
            "This will permanently delete the .llama-cut folder and all of "
            "its contents (context, transcription, frames, storyboard, and "
            "rendered videos) for this project.\n\nThis cannot be undone. "
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        # Style the Yes button as destructive (red text).
        yes_btn = confirm.button(QMessageBox.StandardButton.Yes)
        if yes_btn is not None:
            yes_btn.setStyleSheet(f"color: {COLOR_DANGER}; font-weight: 600;")
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        try:
            shutil.rmtree(app_root, ignore_errors=False)
        except OSError as e:
            QMessageBox.critical(
                self,
                "Clean Up Failed",
                f"Could not remove the .llama-cut folder:\n\n{e}\n\n"
                "Some files may be in use. Close them and try again.",
            )
            return

        self._reset_after_cleanup(folder)

    def _reset_after_cleanup(self, folder: str) -> None:
        """Clear in-memory state and revert to the initial no-folder state.

        The working folder must be cleared *first*: ``set_frames`` /
        ``set_videos`` / ``set_stage`` each call :meth:`persist`, which writes
        back to ``.llama-cut/app_state.json`` — and since the just-deleted
        ``.llama-cut`` tree is gone, any persist while the working folder is
        still set would *recreate* it. Clearing the folder first makes all
        subsequent ``persist()`` calls no-op (they early-return on empty).
        Then clearing the in-memory lists emits the changed signals so the
        UI drops everything; the ``working_folder_changed("")`` signal runs
        ``_on_folder_changed("")`` -> ``set_stage(0)`` -> Welcome page.
        """
        self._state.set_working_folder("")
        self._state.set_frames([])
        self._state.set_videos([])
        self._state.set_stage(0, force=True)

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


def _padded_nav_icon(name: str, glyph_size: int, gap: int, color: str) -> QPixmap:
    """Build a nav icon pixmap with a transparent right margin.

    Renders the Material Symbols glyph at ``glyph_size`` then composes it onto
    a wider transparent canvas (``glyph_size + gap`` wide) so the glyph sits
    at the left and the gap acts as spacing between the icon and the button
    text. QToolButton has no icon-text gap property, so the margin is baked
    into the pixmap and the icon size is set to the full padded width — Qt
    renders it 1:1 (no scaling), keeping the glyph crisp.
    """
    glyph = material_icon_pixmap(name, glyph_size, color)
    out = QPixmap(glyph.width() + gap, glyph.height())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap(0, 0, glyph)
    p.end()
    return out


def _short_path(p: str, max_len: int = 28) -> str:
    if len(p) <= max_len:
        return p
    return "…" + p[-(max_len - 1):]