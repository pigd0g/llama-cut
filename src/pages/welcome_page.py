from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..icons import material_icon
from ..theme import (
    COLOR_ON_SURFACE_VARIANT,
    COLOR_PRIMARY,
    SPACING_MD,
    SPACING_XL,
)


class WelcomePage(QWidget):
    """Empty state: prompt the user to select a working folder."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addStretch()

        center = QHBoxLayout()
        center.addStretch()
        card = QFrame()
        card.setProperty("class", "card")
        card.setFixedWidth(520)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(SPACING_XL, SPACING_XL, SPACING_XL, SPACING_XL)
        cl.setSpacing(SPACING_MD)

        icon = material_icon("video_library", 56, COLOR_PRIMARY)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(icon)

        title = QLabel("Select a Working Folder")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setProperty("class", "headline-lg")
        cl.addWidget(title)

        subtitle = QLabel(
            "Choose a folder containing the videos you want to process. "
            "All extracted frames and metadata will be saved into a hidden "
            ".llama-cut/ subfolder inside it."
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setProperty("class", "body-md")
        subtitle.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        cl.addWidget(subtitle)

        cl.addSpacing(SPACING_MD)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.select_btn = QPushButton("  Select Working Folder")
        self.select_btn.setProperty("class", "primary")
        self.select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_btn.setFixedHeight(40)
        self.select_btn.setMinimumWidth(220)
        self.select_btn.clicked.connect(self._on_select)
        btn_row.addWidget(self.select_btn)
        btn_row.addStretch()
        cl.addLayout(btn_row)

        # Loading indicator (hidden by default)
        self.loading_label = QLabel("Loading…")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setProperty("class", "body-md")
        self.loading_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        self.loading_label.setVisible(False)
        cl.addWidget(self.loading_label)

        center.addWidget(card)
        center.addStretch()
        root.addLayout(center)
        root.addStretch()

    def _on_select(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Working Folder", "",
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
        )
        if not folder:
            return
        # Show the loading indicator and disable the button immediately so
        # the user sees feedback before the (potentially slow) folder scan +
        # state load runs.  Force a repaint so the loading state is visibly
        # drawn on screen, then defer the heavy work with a short delay so the
        # event loop has time to process the paint before we block.
        self.select_btn.setEnabled(False)
        self.loading_label.setVisible(True)
        self.loading_label.repaint()
        QApplication.processEvents()
        QTimer.singleShot(50, lambda: self._apply_folder(folder))

    def _apply_folder(self, folder: str) -> None:
        # set_working_folder triggers _on_folder_changed in app.py which
        # loads state, refreshes stage 1, and forces the stage to 1.
        self._state.set_working_folder(folder)

    def on_enter(self) -> None:
        """Reset to the initial folder-picker state.

        Called when the Welcome stage becomes active (e.g. after a project
        clean-up). Restores the Select button and hides the loading indicator
        left over from a previous folder selection.
        """
        self.select_btn.setEnabled(True)
        self.loading_label.setVisible(False)