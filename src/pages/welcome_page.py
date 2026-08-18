from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
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
            "All extracted frames and metadata will be saved into a "
            "temp/ subfolder inside it."
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

        center.addWidget(card)
        center.addStretch()
        root.addLayout(center)
        root.addStretch()

    def _on_select(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Working Folder", "",
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
        )
        if folder:
            self._state.set_working_folder(folder)
            self._state.set_stage(1)