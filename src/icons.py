from __future__ import annotations

from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel

from .theme import FONT_ICONS


def material_icon(name: str, size: int = 24, color: str | None = None) -> QLabel:
    """Render a Material Symbols icon by name (ligature-based lookup).

    Material Symbols fonts map icon names to glyphs via ligatures, so the
    icon name is used as the label text instead of raw \\ue... codepoints,
    which are not reliably mapped in the Material Symbols Outlined font.

    Font family/size are set via an inline stylesheet because the app-level
    QSS `*` rule (font-family: Inter) overrides QFont set with setFont().
    """
    style = f"font-family: '{FONT_ICONS}'; font-size: {size}px;"
    if color:
        style += f" color: {color};"
    label = QLabel(name)
    label.setStyleSheet(style)
    return label


def material_icon_pixmap(name: str, size: int = 24, color: str | None = None) -> QPixmap:
    """Return a Material Symbols icon as a QPixmap (for use as a QIcon).

    Renders the corresponding :func:`material_icon` QLabel via ``grab()``
    so it can decorate a :class:`QAction`. The label is sized to its
    :func:`sizeHint` before grabbing so the pixmap is not clipped.
    """
    label = material_icon(name, size, color)
    label.setFixedSize(label.sizeHint())
    return label.grab()
