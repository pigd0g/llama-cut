from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)

from ..theme import COLOR_BORDER, COLOR_ON_SURFACE, COLOR_ON_SURFACE_VARIANT


THUMB_W = 192
THUMB_H = 108


class ThumbDelegate(QStyledItemDelegate):
    """Renders a thumbnail pixmap + title + subtitle + checkbox overlay.

    Expected model data (Qt.ItemDataRole.UserRole + DisplayRole):
      - Qt.ItemDataRole.DecorationRole : QPixmap (the thumbnail)
      - Qt.ItemDataRole.UserRole       : {"title": str, "subtitle": str}
    """

    def sizeHint(self, option, index) -> QSize:
        return QSize(THUMB_W + 16, THUMB_H + 48)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        # background on hover/selection
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, _rgba(option.palette, COLOR_BORDER, 0.18))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, _rgba(option.palette, COLOR_BORDER, 0.08))

        rect = option.rect.adjusted(8, 6, -8, -6)
        thumb_rect = QRect(rect.x(), rect.y(), THUMB_W, THUMB_H)

        # Thumbnail
        pix = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pix, QPixmap) and not pix.isNull():
            scaled = pix.scaled(
                THUMB_W, THUMB_H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = thumb_rect.x() + (THUMB_W - scaled.width()) // 2
            y = thumb_rect.y() + (THUMB_H - scaled.height()) // 2
            painter.save()
            painter.setClipRect(thumb_rect)
            painter.drawPixmap(x, y, scaled)
            painter.restore()
        else:
            painter.setBrush(_rgba(option.palette, COLOR_BORDER, 0.4))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(thumb_rect)
            painter.setPen(_qcolor(option.palette, COLOR_ON_SURFACE_VARIANT))
            painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "—")

        # border
        painter.setPen(_qcolor(option.palette, COLOR_BORDER))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(thumb_rect)

        # checkbox indicator (top-left)
        check_rect = QRect(thumb_rect.x() + 6, thumb_rect.y() + 6, 14, 14)
        is_selected = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        painter.setBrush(_qcolor(option.palette, COLOR_ON_SURFACE if is_selected else COLOR_BORDER))
        painter.setPen(_qcolor(option.palette, COLOR_BORDER))
        painter.drawRect(check_rect)
        if is_selected:
            painter.setPen(_qcolor(option.palette, COLOR_ON_SURFACE))
            painter.drawLine(check_rect.x() + 3, check_rect.y() + 7,
                             check_rect.x() + 6, check_rect.y() + 10)
            painter.drawLine(check_rect.x() + 6, check_rect.y() + 10,
                             check_rect.x() + 11, check_rect.y() + 4)

        # text
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        title = data.get("title", "")
        subtitle = data.get("subtitle", "")
        text_top = thumb_rect.bottom() + 6
        painter.setPen(_qcolor(option.palette, COLOR_ON_SURFACE))
        painter.setFont(_font(painter, 12, 600))
        painter.drawText(
            QRect(rect.x(), text_top, rect.width(), 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            _elide(painter, title, rect.width()),
        )
        if subtitle:
            painter.setPen(_qcolor(option.palette, COLOR_ON_SURFACE_VARIANT))
            painter.setFont(_font(painter, 11, 400))
            painter.drawText(
                QRect(rect.x(), text_top + 18, rect.width(), 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _elide(painter, subtitle, rect.width()),
            )
        painter.restore()


# --- helpers ----------------------------------------------------------------

def _qcolor(palette, hex_str: str):
    from ..theme import hex_to_color
    return hex_to_color(hex_str)


def _rgba(palette, hex_str: str, alpha: float):
    c = _qcolor(palette, hex_str)
    c.setAlpha(int(255 * alpha))
    return c


def _font(painter, size: int, weight: int):
    f = QFont(painter.font())
    f.setPointSize(10)
    f.setPixelSize(size)
    try:
        f.setWeight({
            400: QFont.Weight.Normal, 500: QFont.Weight.Medium,
            600: QFont.Weight.DemiBold, 700: QFont.Weight.Bold,
        }.get(weight, QFont.Weight.Normal))
    except Exception:
        pass
    return f


def _elide(painter, text: str, width: int) -> str:
    fm = QFontMetrics(painter.font())
    return fm.elidedText(text, Qt.TextElideMode.ElideRight, width)