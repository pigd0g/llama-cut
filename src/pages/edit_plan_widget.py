"""EditPlanWidget — Linear Edit Plan (LEP) horizontal beat strip.

Per the LEP UX spec:
  - Fixed-height horizontal strip of "beats".
  - Each beat = vertical stack: annotation → 16:9 thumbnail → transition pill.
  - Status-colored borders (green=approved, yellow=draft, red=needs_attention).
  - Horizontal scrolling.
  - V1 scope: core visual + amend via chat (no drag/snap/minimap/hover-GIF).
  - Thumbnails extracted on-the-fly at beat midpoint, cached.

Amendments (reorder, insert, delete, transition change) go through the chat
→ agent calls update_edit_plan → LEP re-renders.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal, QEvent
from PyQt6.QtGui import QFontMetrics, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..theme import (
    COLOR_BORDER,
    COLOR_DANGER,
    COLOR_ON_SURFACE,
    COLOR_ON_SURFACE_VARIANT,
    COLOR_PRIMARY,
    COLOR_SUCCESS,
    COLOR_SURFACE,
    COLOR_SURFACE_CONTAINER,
    COLOR_WARNING,
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_FULL,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XS,
)


# Beat status → border color
_STATUS_COLORS = {
    "approved": COLOR_SUCCESS,
    "draft": COLOR_WARNING,
    "needs_attention": COLOR_DANGER,
}

_THUMB_W = 192   # 16:9 at 108px height
_THUMB_H = 108
# Beat card vertical budget. The annotation wraps (multi-line) but is
# truncated to a char budget with an ellipsis so long descriptions don't
# push the card height unbounded. The full text is available via the
# tooltip on hover (set on the card frame so hovering anywhere works).
_ANN_HEIGHT = 56    # annotation zone (purpose + description, wrapped)
_INFO_HEIGHT = 52   # beat id + source + time range (3 HTML lines)
_ANN_CHAR_LIMIT = 50  # max chars shown in the annotation before ellipsis
_INFO_CHAR_LIMIT = 24  # max chars for source filename in the info label
# Total card height = padding(8*2) + ann(56) + thumb(108) + info(52) + spacing(4*2) = 248
# Scroll area needs card height + top/bottom margins (8*2) + a buffer so
# the vertical scrollbar never appears.
_SCROLL_MIN_HEIGHT = 248 + 16 + 12


class EditPlanWidget(QWidget):
    """Horizontal beat strip displaying the edit plan's timeline.

    Signals:
      - beat_clicked(str): the user clicked a beat (emits beat id).
    """

    beat_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._beat_widgets: dict[str, _BeatCard] = {}
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACING_XS)

        # Header row
        header = QHBoxLayout()
        header.setContentsMargins(SPACING_MD, 0, SPACING_MD, 0)
        self._title_label = QLabel("Edit Plan")
        self._title_label.setStyleSheet(
            f"color: {COLOR_ON_SURFACE}; font-weight: 600; font-size: 14px;"
        )
        header.addWidget(self._title_label)
        header.addStretch()
        self._count_label = QLabel("0 beats")
        self._count_label.setStyleSheet(
            f"color: {COLOR_ON_SURFACE_VARIANT}; font-size: 12px;"
        )
        header.addWidget(self._count_label)
        root.addLayout(header)

        # Horizontal scroll area for beats
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"""
            QScrollArea {{ background-color: {COLOR_SURFACE}; border: none; }}
            QScrollBar:horizontal {{
                height: 6px; background: transparent;
            }}
            QScrollBar::handle:horizontal {{
                background: {COLOR_BORDER}; border-radius: 3px; min-width: 32px;
            }}
        """)
        self.scroll.setMinimumHeight(_SCROLL_MIN_HEIGHT)
        # No fixed height — let the strip scale with its contents, with a
        # sensible minimum so single-beat plans don't look cramped and the
        # vertical scrollbar never triggers.

        self._beats_container = QWidget()
        self._beats_container.setStyleSheet(f"background-color: {COLOR_SURFACE};")
        self._beats_layout = QHBoxLayout(self._beats_container)
        self._beats_layout.setContentsMargins(SPACING_MD, SPACING_SM, SPACING_MD, SPACING_SM)
        self._beats_layout.setSpacing(SPACING_SM)
        self._beats_layout.addStretch()
        self.scroll.setWidget(self._beats_container)
        root.addWidget(self.scroll)

        # Empty state
        self._empty_label = QLabel("No beats yet. Chat with the agent to build your edit plan.")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(
            f"color: {COLOR_ON_SURFACE_VARIANT}; font-size: 14px; "
            f"padding: {SPACING_LG}px;"
        )
        self._empty_label.setVisible(False)
        root.addWidget(self._empty_label)

    def update_plan(self, plan) -> None:
        """Rebuild the beat strip from an EditPlan."""
        # Clear existing
        self._beat_widgets.clear()
        while self._beats_layout.count() > 1:
            item = self._beats_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        timeline = plan.timeline if plan else []
        if not timeline:
            self._empty_label.setVisible(True)
            self.scroll.setVisible(False)
            self._count_label.setText("0 beats")
            return

        self._empty_label.setVisible(False)
        self.scroll.setVisible(True)
        self._count_label.setText(f"{len(timeline)} beats")

        for i, beat in enumerate(timeline):
            card = _BeatCard(beat)
            card.clicked.connect(lambda checked, bid=beat.id: self.beat_clicked.emit(bid))
            self._beat_widgets[beat.id] = card
            self._beats_layout.insertWidget(self._beats_layout.count() - 1, card)

            # Transition pill between this beat and the next (not after the last)
            if i < len(timeline) - 1 and beat.transition_out:
                pill = _TransitionPill(beat.transition_out, beat.transition_duration)
                self._beats_layout.insertWidget(self._beats_layout.count() - 1, pill)

    def set_beat_thumbnail(self, beat_id: str, thumbnail_path: str) -> None:
        """Update a beat's thumbnail image."""
        card = self._beat_widgets.get(beat_id)
        if card is not None:
            card.set_thumbnail(thumbnail_path)


class _BeatCard(QFrame):
    """A single beat in the LEP: annotation → thumbnail → (no pill, pill is separate)."""

    clicked = pyqtSignal(bool)

    def __init__(self, beat, parent=None):
        super().__init__(parent)
        self._beat = beat
        self._build()

    def _build(self) -> None:
        status_color = _STATUS_COLORS.get(self._beat.status, _STATUS_COLORS["draft"])
        self.setStyleSheet(f"""
            _BeatCard {{
                background-color: {COLOR_SURFACE_CONTAINER};
                border: 2px solid {status_color};
                border-radius: {RADIUS_MD}px;
                min-width: {_THUMB_W + 16}px;
                max-width: {_THUMB_W + 16}px;
            }}
            _BeatCard:hover {{
                border-color: {COLOR_PRIMARY};
            }}
        """)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        # Build the full text shown in the tooltip on hover. We use
        # QToolTip.showText() in enterEvent instead of setToolTip() because
        # the frame's child widgets and stylesheet interfere with Qt's
        # built-in tooltip delivery on hover.
        annotation = self._annotation_text()
        time_range = f"{self._beat.source_start:.1f}-{self._beat.source_end:.1f}s"
        self._tooltip_text = (
            f"{self._beat.id}\n"
            f"{annotation}\n"
            f"Source: {self._beat.source}\n"
            f"Range: {time_range}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(SPACING_SM, SPACING_SM, SPACING_SM, SPACING_SM)
        lay.setSpacing(SPACING_XS)

        # Annotation zone (top): wrap to multiple lines but truncate to a
        # char budget with an ellipsis so long descriptions don't grow the
        # card unbounded. The full text is shown in the card tooltip on hover.
        truncated = self._truncate(annotation, _ANN_CHAR_LIMIT)
        ann_label = QLabel(truncated)
        ann_label.setWordWrap(True)
        ann_label.setStyleSheet(
            f"color: {COLOR_ON_SURFACE}; font-size: 12px; "
            f"background: transparent; border: none; padding: 0;"
        )
        ann_label.setFixedHeight(_ANN_HEIGHT)
        lay.addWidget(ann_label)

        # Thumbnail (middle, 16:9)
        self._thumb_label = QLabel()
        self._thumb_label.setFixedSize(_THUMB_W, _THUMB_H)
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setText("...")
        self._thumb_label.setStyleSheet(
            f"QLabel {{ background-color: #1a1a1a; color: {COLOR_ON_SURFACE_VARIANT}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD - 2}px; "
            f"font-size: 24px; }}"
        )
        lay.addWidget(self._thumb_label)

        # Beat id + source + time range (bottom): truncate the source name
        # with an ellipsis; full details shown in the card tooltip on hover.
        src_trunc = self._truncate(self._beat.source, _INFO_CHAR_LIMIT)
        info_label = QLabel(
            f"<b>{self._elide_html(self._beat.id, 20)}</b><br>"
            f"<span style='color: {COLOR_ON_SURFACE_VARIANT}; font-size: 11px;'>"
            f"{self._elide_html(src_trunc, _INFO_CHAR_LIMIT)}</span><br>"
            f"<span style='color: {COLOR_ON_SURFACE_VARIANT}; font-size: 11px;'>"
            f"{time_range}</span>"
        )
        info_label.setStyleSheet("background: transparent; border: none; padding: 0;")
        info_label.setFixedHeight(_INFO_HEIGHT)
        lay.addWidget(info_label)

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        """Truncate text to max_chars, appending an ellipsis if cut."""
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars - 1] + "\u2026"

    @staticmethod
    def _elide_html(text: str, max_chars: int) -> str:
        """Elide text for use inside HTML, escaping special chars first."""
        escaped = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if len(escaped) <= max_chars:
            return escaped
        return escaped[:max_chars - 1] + "\u2026"

    def _annotation_text(self) -> str:
        parts = []
        if self._beat.purpose:
            parts.append(self._beat.purpose)
        if self._beat.description:
            parts.append(self._beat.description)
        return " — ".join(parts) if parts else self._beat.id

    def set_thumbnail(self, path: str) -> None:
        """Load a thumbnail image from a file path."""
        if path and Path(path).exists():
            pix = QPixmap(path)
            if not pix.isNull():
                scaled = pix.scaled(
                    _THUMB_W, _THUMB_H,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                # Crop to exact size if expanded
                if scaled.width() > _THUMB_W or scaled.height() > _THUMB_H:
                    scaled = scaled.copy(
                        (scaled.width() - _THUMB_W) // 2,
                        (scaled.height() - _THUMB_H) // 2,
                        _THUMB_W, _THUMB_H,
                    )
                self._thumb_label.setPixmap(scaled)
                self._thumb_label.setText("")

    def mousePressEvent(self, event) -> None:
        self.clicked.emit(True)
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        """Show the full beat text as a tooltip when the mouse enters the card.

        Uses QToolTip.showText() directly because setToolTip() on the frame
        does not reliably fire — the frame's child widgets and stylesheet
        interfere with Qt's built-in hover-tooltip delivery.
        """
        tip = getattr(self, "_tooltip_text", "")
        if tip:
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """Hide the tooltip when the mouse leaves the card."""
        QToolTip.hideText()
        super().leaveEvent(event)


class _TransitionPill(QFrame):
    """A rounded pill between two beats showing the transition type."""

    # Solid green background with white text per the design spec.
    _GREEN = "#22c55e"

    def __init__(self, transition_type: str, duration: float = 0.0, parent=None):
        super().__init__(parent)
        label_text = transition_type
        if duration > 0 and transition_type != "cut":
            label_text = f"{transition_type} {duration:.1f}s"
        lbl = QLabel(label_text)
        lbl.setStyleSheet(
            f"color: #ffffff; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SPACING_SM, 0, SPACING_SM, 0)
        lay.setSpacing(0)
        lay.addWidget(lbl)
        self.setStyleSheet(f"""
            _TransitionPill {{
                background-color: {self._GREEN};
                border: none;
                border-radius: {RADIUS_FULL}px;
                min-height: 24px;
                max-height: 24px;
            }}
        """)
        self.setFixedWidth(80 if duration > 0 and transition_type != "cut" else 60)