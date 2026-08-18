from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QFont, QFontDatabase

# --- Design Tokens (Dark mode only for phase 1) ----------------------------

FONT_HEADING = "Hanken Grotesk"
FONT_BODY = "Inter"
FONT_ICONS = "Material Symbols Outlined"

TYPO_HEADLINE_LG = (24, 600, 32, -0.01)
TYPO_HEADLINE_MD = (20, 600, 28, -0.01)
TYPO_HEADLINE_SM = (16, 600, 24, 0.00)
TYPO_BODY_LG = (15, 400, 22, 0.00)
TYPO_BODY_MD = (14, 400, 20, 0.00)
TYPO_BODY_SM = (13, 400, 18, 0.00)
TYPO_LABEL_MD = (12, 600, 16, 0.05)
TYPO_LABEL_SM = (11, 500, 14, 0.00)

SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 16
SPACING_LG = 24
SPACING_XL = 32
SPACING_CONTAINER_MARGIN = 24
SPACING_GUTTER = 16

RADIUS_SM = 2
RADIUS_DEFAULT = 4
RADIUS_MD = 6
RADIUS_LG = 8
RADIUS_XL = 12
RADIUS_FULL = 9999

# Dark mode palette
COLOR_BACKGROUND = "#0B121F"
COLOR_SURFACE = "#161C27"
COLOR_SURFACE_CONTAINER = "#1E2738"
COLOR_SURFACE_HIGH = "#252D3D"
COLOR_SURFACE_LOW = "#11161F"
COLOR_SURFACE_LOWEST = "#161C27"
COLOR_BORDER = "#252D3D"
COLOR_OUTLINE = "#3d4560"
COLOR_OUTLINE_VARIANT = "#252D3D"
COLOR_ON_SURFACE = "#edf0ff"
COLOR_ON_SURFACE_VARIANT = "#9ca3b8"
COLOR_PRIMARY = "#b2c5ff"
COLOR_PRIMARY_CONTAINER = "#0040a2"
COLOR_ON_PRIMARY = "#0B121F"
COLOR_SECONDARY = "#9ca3b8"
COLOR_SUCCESS = "#1e7d4a"
COLOR_SUCCESS_BG = "#0d2a1a"
COLOR_WARNING = "#b45309"
COLOR_WARNING_BG = "#2a1a0a"
COLOR_DANGER = "#ef4444"
COLOR_DANGER_BG = "#2a0d0d"
COLOR_ACCENT = "#60a5fa"

# Stage status chip colors
COLOR_PENDING = "#9ca3b8"
COLOR_ACTIVE = "#60a5fa"
COLOR_DONE = "#1e7d4a"
COLOR_ERROR = "#ef4444"


def _font(family: str, size: int, weight: int = 400) -> QFont:
    f = QFont(family, size)
    f.setWeight(QFont.Weight.WeightMap.get(weight, QFont.Weight.Normal) if hasattr(QFont, "Weight") else QFont.Weight.Normal)
    try:
        f.setWeight({
            400: QFont.Weight.Normal,
            500: QFont.Weight.Medium,
            600: QFont.Weight.DemiBold,
            700: QFont.Weight.Bold,
        }.get(weight, QFont.Weight.Normal))
    except Exception:
        pass
    return f


def apply_app_fonts() -> None:
    """Best-effort: register bundled/installed fonts. Falls back silently."""
    for family in (FONT_HEADING, FONT_BODY, FONT_ICONS):
        if QFontDatabase.hasFamily(family):
            continue


def register_fonts() -> None:
    """Register bundled font files (assets/fonts) with the application."""
    fonts_dir = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    if not fonts_dir.is_dir():
        return
    for font_file in sorted(fonts_dir.glob("*.ttf")) + sorted(fonts_dir.glob("*.otf")):
        QFontDatabase.addApplicationFont(str(font_file))


# --- QSS --------------------------------------------------------------------

DARK_QSS = f"""
* {{
    font-family: '{FONT_BODY}', 'Segoe UI', sans-serif;
    font-size: {TYPO_BODY_MD[0]}px;
    color: {COLOR_ON_SURFACE};
    outline: none;
    border: none;
    padding: 0;
    margin: 0;
    background: transparent;
}}

QMainWindow, QDialog {{
    background-color: {COLOR_BACKGROUND};
}}

QWidget {{
    background-color: transparent;
}}

/* Scrollbars */
QScrollBar:vertical {{
    width: 8px;
    background: transparent;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {COLOR_OUTLINE};
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: {COLOR_ON_SURFACE_VARIANT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
    height: 0;
    border: none;
}}
QScrollBar:horizontal {{
    height: 8px;
    background: transparent;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {COLOR_OUTLINE};
    border-radius: 4px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {COLOR_ON_SURFACE_VARIANT};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
    width: 0;
    border: none;
}}

/* Labels */
QLabel {{
    color: {COLOR_ON_SURFACE};
    background: transparent;
}}
QLabel[class="muted"] {{
    color: {COLOR_ON_SURFACE_VARIANT};
}}
QLabel[class="headline-lg"] {{
    font-size: {TYPO_HEADLINE_LG[0]}px;
    font-weight: 600;
}}
QLabel[class="headline-md"] {{
    font-size: {TYPO_HEADLINE_MD[0]}px;
    font-weight: 600;
}}
QLabel[class="headline-sm"] {{
    font-size: {TYPO_HEADLINE_SM[0]}px;
    font-weight: 600;
}}
QLabel[class="body-lg"] {{
    font-size: {TYPO_BODY_LG[0]}px;
}}
QLabel[class="body-md"] {{
    font-size: {TYPO_BODY_MD[0]}px;
}}
QLabel[class="body-sm"] {{
    font-size: {TYPO_BODY_SM[0]}px;
    color: {COLOR_ON_SURFACE_VARIANT};
}}
QLabel[class="label-md"] {{
    font-size: {TYPO_LABEL_MD[0]}px;
    font-weight: 600;
    letter-spacing: {TYPO_LABEL_MD[3]}em;
    color: {COLOR_ON_SURFACE_VARIANT};
}}
QLabel[class="label-sm"] {{
    font-size: {TYPO_LABEL_SM[0]}px;
    font-weight: 500;
    color: {COLOR_ON_SURFACE_VARIANT};
}}

/* Cards */
QFrame[class="card"] {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_LG}px;
}}

QFrame[class="divider"] {{
    background: {COLOR_BORDER};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

/* Sidebar */
#sidebar {{
    background-color: {COLOR_SURFACE_LOW};
    border-right: 1px solid {COLOR_BORDER};
}}

QToolButton#sidebarNavItem {{
    background: transparent;
    border: none;
    border-radius: {RADIUS_DEFAULT}px;
    padding: {SPACING_SM}px {SPACING_MD}px;
    text-align: left;
    color: {COLOR_ON_SURFACE_VARIANT};
    font-size: {TYPO_BODY_MD[0]}px;
    font-weight: 500;
}}
QToolButton#sidebarNavItem::menu-indicator {{
    image: none;
}}
QToolButton#sidebarNavItem:hover {{
    background-color: {COLOR_SURFACE_CONTAINER};
    color: {COLOR_ON_SURFACE};
}}
QToolButton#sidebarNavItem[active="true"] {{
    background-color: {COLOR_SURFACE_CONTAINER};
    color: {COLOR_PRIMARY};
    font-weight: 600;
}}
QToolButton#sidebarNavItem:disabled {{
    color: {COLOR_OUTLINE};
}}

/* Buttons */
QPushButton {{
    background-color: {COLOR_SURFACE_CONTAINER};
    color: {COLOR_ON_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_DEFAULT}px;
    padding: {SPACING_SM}px {SPACING_MD}px;
    font-size: {TYPO_LABEL_MD[0]}px;
    font-weight: 600;
    letter-spacing: {TYPO_LABEL_MD[3]}em;
}}
QPushButton:hover {{
    background-color: {COLOR_SURFACE_HIGH};
    border-color: {COLOR_OUTLINE};
}}
QPushButton:pressed {{
    background-color: {COLOR_SURFACE};
}}
QPushButton:disabled {{
    color: {COLOR_OUTLINE};
    background-color: {COLOR_SURFACE_LOW};
    border-color: {COLOR_BORDER};
}}
QPushButton[class="primary"] {{
    background-color: {COLOR_PRIMARY_CONTAINER};
    color: {COLOR_ON_SURFACE};
    border: 1px solid {COLOR_PRIMARY_CONTAINER};
}}
QPushButton[class="primary"]:hover {{
    background-color: {COLOR_ACCENT};
    border-color: {COLOR_ACCENT};
}}
QPushButton[class="primary"]:pressed {{
    background-color: {COLOR_PRIMARY_CONTAINER};
}}
QPushButton[class="primary"]:disabled {{
    background-color: {COLOR_SURFACE_CONTAINER};
    color: {COLOR_OUTLINE};
    border-color: {COLOR_BORDER};
}}
QPushButton[class="ghost"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {COLOR_ON_SURFACE_VARIANT};
}}
QPushButton[class="ghost"]:hover {{
    background-color: {COLOR_SURFACE_CONTAINER};
    color: {COLOR_ON_SURFACE};
}}
QPushButton[class="danger"] {{
    background-color: {COLOR_DANGER};
    color: #ffffff;
    border: 1px solid {COLOR_DANGER};
}}
QPushButton[class="danger"]:hover {{
    background-color: #dc2626;
}}

/* Radio + Check */
QRadioButton, QCheckBox {{
    color: {COLOR_ON_SURFACE};
    spacing: {SPACING_SM}px;
    background: transparent;
}}
QRadioButton::indicator, QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {COLOR_OUTLINE};
    border-radius: 3px;
    background: {COLOR_SURFACE_CONTAINER};
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QRadioButton::indicator:checked, QCheckBox::indicator:checked {{
    background: {COLOR_PRIMARY_CONTAINER};
    border-color: {COLOR_PRIMARY_CONTAINER};
}}
QRadioButton::indicator:hover, QCheckBox::indicator:hover {{
    border-color: {COLOR_PRIMARY};
}}

/* ComboBox */
QComboBox {{
    background-color: {COLOR_SURFACE_CONTAINER};
    color: {COLOR_ON_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_DEFAULT}px;
    padding: {SPACING_SM}px {SPACING_MD}px;
    min-height: 20px;
}}
QComboBox:hover {{
    border-color: {COLOR_OUTLINE};
}}
QComboBox::drop-down {{
    width: 24px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {COLOR_ON_SURFACE_VARIANT};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_ON_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_DEFAULT}px;
    selection-background-color: {COLOR_SURFACE_CONTAINER};
    selection-color: {COLOR_PRIMARY};
    padding: {SPACING_XS}px;
    outline: none;
}}

/* LineEdit / SpinBox */
QLineEdit, QSpinBox, QTextEdit, QPlainTextEdit {{
    background-color: {COLOR_SURFACE_CONTAINER};
    color: {COLOR_ON_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_SM}px;
    padding: {SPACING_SM}px {SPACING_MD}px;
    selection-background-color: {COLOR_PRIMARY_CONTAINER};
    selection-color: {COLOR_ON_SURFACE};
}}
QLineEdit:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {COLOR_PRIMARY};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 16px;
}}
QSpinBox::up-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {COLOR_ON_SURFACE_VARIANT};
}}
QSpinBox::down-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {COLOR_ON_SURFACE_VARIANT};
}}

/* ProgressBar */
QProgressBar {{
    background-color: {COLOR_SURFACE_CONTAINER};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_SM}px;
    text-align: center;
    color: {COLOR_ON_SURFACE};
    height: 18px;
}}
QProgressBar::chunk {{
    background-color: {COLOR_PRIMARY_CONTAINER};
    border-radius: {RADIUS_SM}px;
}}

/* List/Table views */
QListView, QTableView, QTreeView {{
    background-color: {COLOR_BACKGROUND};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_LG}px;
    outline: none;
}}
QListView::item, QTableView::item {{
    color: {COLOR_ON_SURFACE};
    padding: {SPACING_XS}px;
}}
QHeaderView::section {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_ON_SURFACE_VARIANT};
    padding: {SPACING_SM}px {SPACING_MD}px;
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    font-size: {TYPO_LABEL_MD[0]}px;
    font-weight: 600;
    letter-spacing: {TYPO_LABEL_MD[3]}em;
}}
QTableView QTableCornerButton::section {{
    background-color: {COLOR_SURFACE};
    border: none;
}}

/* Tooltip */
QToolTip {{
    background-color: {COLOR_SURFACE_HIGH};
    color: {COLOR_ON_SURFACE};
    border: 1px solid {COLOR_OUTLINE};
    border-radius: {RADIUS_SM}px;
    padding: {SPACING_XS}px {SPACING_SM}px;
}}
"""


def hex_to_color(h: str) -> QColor:
    h = h.lstrip("#")
    return QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))