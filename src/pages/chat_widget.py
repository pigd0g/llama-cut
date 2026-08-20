"""ChatWidget — reusable chat UI with message bubbles, thinking animation,
and expandable tool-call chips.

Per the Chat UX spec:
  - Deep charcoal background, high-contrast white text.
  - User messages: right-aligned rounded pills.
  - Assistant messages: left-aligned with bold sender label.
  - Tool calls: compact chips (click to expand details).
  - Thinking indicator: animated pulsing dots while waiting for the LLM.
  - Input bar: floating rounded bar with send arrow button + decorative
    model selector label.

Row types (all subclass _ChatRow):
  - AssistantRow: bold sender label + markdown-rendered text.
  - UserRow: right-aligned rounded pill with plain text.
  - ThinkingRow: bold sender label + animated pulsing dots.
  - ToolChipRow: collapsible tool_name(args) + expandable details panel.
"""
from __future__ import annotations

import json

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..context_review import markdown_to_html
from ..theme import (
    COLOR_BORDER,
    COLOR_DANGER,
    COLOR_ON_SURFACE,
    COLOR_ON_SURFACE_VARIANT,
    COLOR_PRIMARY,
    COLOR_PRIMARY_CONTAINER,
    COLOR_SURFACE,
    COLOR_SURFACE_CONTAINER,
    COLOR_SURFACE_HIGH,
    COLOR_SUCCESS,
    FONT_BODY,
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_FULL,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XS,
)


# --- Chat message data classes ------------------------------------------------

class ChatMessage:
    """A single message in the chat transcript (for restore/persist)."""
    def __init__(self, role: str, content: str = "",
                 tool_name: str = "", tool_args: str = "",
                 tool_result: str = "", tool_success: bool = True,
                 tool_duration: float = 0.0):
        self.role = role            # "user", "assistant", "tool"
        self.content = content
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_result = tool_result
        self.tool_success = tool_success
        self.tool_duration = tool_duration

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content}
        if self.role == "tool":
            d.update({
                "tool_name": self.tool_name,
                "tool_args": self.tool_args,
                "tool_result": self.tool_result,
                "tool_success": self.tool_success,
                "tool_duration": self.tool_duration,
            })
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ChatMessage":
        return cls(
            role=d.get("role", "assistant"),
            content=d.get("content", ""),
            tool_name=d.get("tool_name", ""),
            tool_args=d.get("tool_args", ""),
            tool_result=d.get("tool_result", ""),
            tool_success=d.get("tool_success", True),
            tool_duration=d.get("tool_duration", 0.0),
        )


# --- Base row -----------------------------------------------------------------

class _ChatRow(QFrame):
    """Base class for all chat message rows."""


# --- Assistant row ------------------------------------------------------------

class AssistantRow(_ChatRow):
    """Left-aligned assistant message with bold sender label + markdown text."""

    def __init__(self, content: str, sender: str = "assistant", parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            _ChatRow {{
                background: transparent;
                border: none;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SPACING_MD, SPACING_XS, SPACING_MD, SPACING_XS)
        lay.setSpacing(SPACING_XS)

        label = QLabel(sender)
        label.setStyleSheet(
            f"color: {COLOR_PRIMARY}; font-weight: 700; "
            f"font-size: 13px;"
        )
        lay.addWidget(label)

        text_browser = QTextBrowser()
        text_browser.setOpenExternalLinks(False)
        text_browser.setHtml(markdown_to_html(content) if content else "<i>...</i>")
        text_browser.setStyleSheet(f"""
            QTextBrowser {{
                background-color: {COLOR_SURFACE_CONTAINER};
                color: {COLOR_ON_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MD}px;
                padding: {SPACING_SM}px {SPACING_MD}px;
                font-size: 14px;
            }}
        """)
        text_browser.setFixedHeight(_estimate_browser_height(text_browser, content))
        lay.addWidget(text_browser)


# --- User row -----------------------------------------------------------------

class UserRow(_ChatRow):
    """Right-aligned user message in a rounded pill."""

    def __init__(self, content: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SPACING_MD, SPACING_XS, SPACING_MD, SPACING_XS)
        lay.setSpacing(0)

        # Right-aligning container: stretch on the left pushes the pill right.
        right_lay = QHBoxLayout()
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)
        right_lay.addStretch(1)

        pill = QLabel(content)
        pill.setWordWrap(True)
        pill.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pill.setMaximumWidth(480)
        pill.setAlignment(Qt.AlignmentFlag.AlignRight)
        pill.setStyleSheet(f"""
            QLabel {{
                background-color: {COLOR_PRIMARY_CONTAINER};
                color: {COLOR_ON_SURFACE};
                border: 1px solid {COLOR_PRIMARY_CONTAINER};
                border-radius: {RADIUS_LG}px;
                padding: {SPACING_SM}px {SPACING_MD}px;
                font-size: 14px;
            }}
        """)
        right_lay.addWidget(pill)
        lay.addLayout(right_lay)


# --- Thinking row (pulsing dots) ----------------------------------------------

class ThinkingRow(_ChatRow):
    """Left-aligned row with bold sender label + animated pulsing dots."""

    def __init__(self, sender: str = "assistant", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SPACING_MD, SPACING_XS, SPACING_MD, SPACING_XS)
        lay.setSpacing(SPACING_XS)

        label = QLabel(sender)
        label.setStyleSheet(
            f"color: {COLOR_PRIMARY}; font-weight: 700; font-size: 13px;"
        )
        lay.addWidget(label)

        self._dots_label = QLabel("•    •    •")
        self._dots_label.setStyleSheet(
            f"color: {COLOR_ON_SURFACE_VARIANT}; font-size: 18px; "
            f"font-weight: 700; padding: {SPACING_SM}px {SPACING_MD}px;"
        )
        lay.addWidget(self._dots_label)

        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._frame = 0
        self._timer.timeout.connect(self._animate)
        self._timer.start()

    def _animate(self) -> None:
        """Cycle dot opacities to create a pulsing wave effect."""
        intensities = [
            [1.0, 0.3, 0.3],
            [0.6, 1.0, 0.3],
            [0.3, 0.6, 1.0],
        ]
        i = self._frame % len(intensities)
        row = intensities[i]
        # Render each dot with its opacity via a simple HTML span approach.
        dots_html = (
            f"<span style='opacity:{row[0]:.1f};color:{COLOR_ON_SURFACE}'>●</span>"
            f"&nbsp;&nbsp;"
            f"<span style='opacity:{row[1]:.1f};color:{COLOR_ON_SURFACE}'>●</span>"
            f"&nbsp;&nbsp;"
            f"<span style='opacity:{row[2]:.1f};color:{COLOR_ON_SURFACE}'>●</span>"
        )
        self._dots_label.setText(dots_html)
        self._frame += 1

    def stop(self) -> None:
        self._timer.stop()


# --- Tool chip row (expandable) -----------------------------------------------

class ToolChipRow(_ChatRow):
    """Collapsible tool-call chip: click to expand args/result details."""

    def __init__(self, tool_name: str, args_str: str, result_str: str,
                 success: bool, duration: float, parent=None):
        super().__init__(parent)
        self._expanded = False
        self._tool_name = tool_name
        self._args_str = args_str
        self._result_str = result_str
        self._success = success
        self._duration = duration

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(SPACING_MD, SPACING_XS, SPACING_MD, SPACING_XS)
        self._layout.setSpacing(SPACING_XS)

        # Chip header (clickable)
        self._header_btn = QPushButton(self._collapsed_label())
        self._header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_btn.setCheckable(True)
        self._header_btn.setStyleSheet(self._chip_style())
        self._header_btn.clicked.connect(self._toggle)
        self._layout.addWidget(self._header_btn)

        # Details panel (hidden by default)
        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        mono = QFont("Cascadia Code", 9)
        if not mono.exactMatch():
            mono = QFont("Consolas", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._details.setFont(mono)
        self._details.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {COLOR_SURFACE};
                color: {COLOR_ON_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MD}px;
                padding: {SPACING_SM}px;
                font-family: 'Cascadia Code', 'Consolas', monospace;
            }}
        """)
        self._details.setPlainText(self._details_text())
        self._details.setFixedHeight(160)
        self._details.setVisible(False)
        self._layout.addWidget(self._details)

    def _collapsed_label(self) -> str:
        arrow = "▸"
        summary = _tool_args_summary(self._tool_name, self._args_str)
        status = "✓" if self._success else "✗"
        return f"  {arrow}  {self._tool_name}({summary})  {status}  {self._duration:.1f}s"

    def _chip_style(self) -> str:
        color = COLOR_SUCCESS if self._success else COLOR_DANGER
        bg = f"{color}22" if self._success else f"{COLOR_DANGER}22"
        return f"""
            QPushButton {{
                background-color: {bg};
                color: {COLOR_ON_SURFACE};
                border: 1px solid {color}66;
                border-radius: {RADIUS_MD}px;
                padding: {SPACING_XS}px {SPACING_SM}px;
                font-size: 13px;
                font-family: 'Cascadia Code', 'Consolas', monospace;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {color}33;
            }}
        """

    def _details_text(self) -> str:
        try:
            args_pretty = json.dumps(json.loads(self._args_str), indent=2)
        except (json.JSONDecodeError, TypeError):
            args_pretty = self._args_str
        try:
            result_pretty = json.dumps(json.loads(self._result_str), indent=2)
        except (json.JSONDecodeError, TypeError):
            result_pretty = self._result_str
        return (
            f"args:\n{args_pretty}\n\n"
            f"result:\n{result_pretty}\n\n"
            f"duration: {self._duration:.2f}s  success: {self._success}"
        )

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._details.setVisible(self._expanded)
        label = self._collapsed_label()
        if self._expanded:
            label = label.replace("▸", "▾")
        self._header_btn.setText(label)


def _tool_args_summary(tool_name: str, args_str: str) -> str:
    """Build a short summary of tool args for the collapsed chip label."""
    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, TypeError):
        return "..."
    if tool_name in ("probe_video",):
        return args.get("video_path", "?")
    if tool_name in ("inspect_clip",):
        v = args.get("video_path", "?")
        s = args.get("start_time", "?")
        e = args.get("end_time", "?")
        return f"{v}, {s}, {e}"
    if tool_name in ("commit_edit_plan", "update_edit_plan"):
        plan = args.get("plan", {})
        beats = len(plan.get("timeline", []))
        cmds = len(plan.get("commands", []))
        return f"{beats} beats · {cmds} commands"
    return args_str[:60] + "..." if len(args_str) > 60 else args_str


def _estimate_browser_height(browser: QTextBrowser, content: str) -> int:
    """Estimate a fixed height for a text browser based on content length."""
    if not content:
        return 40
    lines = content.count("\n") + 1
    # Rough: ~20px per line, min 40, max 300.
    h = max(40, min(300, lines * 20 + 20))
    return h


# --- Chat widget ---------------------------------------------------------------

class ChatWidget(QWidget):
    """Full chat UI: scrollable message list + floating input bar.

    Signals:
      - message_sent(str): the user clicked send (or pressed Ctrl+Return).
    """

    message_sent = pyqtSignal(str)

    def __init__(self, model_label: str = "", parent=None):
        super().__init__(parent)
        self._model_label = model_label
        self._thinking_row: ThinkingRow | None = None
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Message scroll area
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet(f"QScrollArea {{ background-color: {COLOR_SURFACE}; }}")
        self._messages_container = QWidget()
        self._messages_container.setStyleSheet(
            f"background-color: {COLOR_SURFACE};"
        )
        self._messages_layout = QVBoxLayout(self._messages_container)
        self._messages_layout.setContentsMargins(0, 0, 0, 0)
        self._messages_layout.setSpacing(SPACING_XS)
        self._messages_layout.addStretch()
        self.scroll.setWidget(self._messages_container)
        root.addWidget(self.scroll, 1)

        # Input bar (floating rounded bar)
        input_bar = QFrame()
        input_bar.setStyleSheet(f"""
            QFrame {{
                background-color: {COLOR_SURFACE_HIGH};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_LG}px;
                margin: {SPACING_SM}px;
            }}
        """)
        input_lay = QHBoxLayout(input_bar)
        input_lay.setContentsMargins(SPACING_MD, SPACING_SM, SPACING_SM, SPACING_SM)
        input_lay.setSpacing(SPACING_SM)

        # Model selector (decorative label + dropdown arrow icon)
        if self._model_label:
            model_lbl = QLabel(f"  {self._model_label}  ▾")
            model_lbl.setStyleSheet(
                f"color: {COLOR_ON_SURFACE_VARIANT}; font-size: 13px; "
                f"font-weight: 500; padding: 0 {SPACING_XS}px;"
            )
            input_lay.addWidget(model_lbl)

        # Separator
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background-color: {COLOR_BORDER};")
        sep.setFixedHeight(24)
        input_lay.addWidget(sep)

        # Text input
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Type a message...  (Ctrl+Enter to send)")
        self.input.setStyleSheet(f"""
            QPlainTextEdit {{
                background: transparent;
                border: none;
                color: {COLOR_ON_SURFACE};
                font-size: 14px;
                padding: {SPACING_XS}px 0;
            }}
        """)
        _line_h = int(self.input.fontMetrics().lineSpacing())
        self.input.setFixedHeight(3 * _line_h + 8)
        input_lay.addWidget(self.input, 1)

        # Send button (circular, Material Symbols arrow_upward icon)
        self.send_btn = QPushButton("arrow_upward")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setFixedSize(36, 36)
        self.send_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {COLOR_PRIMARY};
                color: {COLOR_ON_SURFACE};
                border: none;
                border-radius: {RADIUS_FULL}px;
                font-family: 'Material Symbols Outlined';
                font-size: 20px;
                padding: 0px;
            }}
            QPushButton:hover {{
                background-color: {COLOR_PRIMARY}cc;
            }}
            QPushButton:disabled {{
                background-color: {COLOR_SURFACE_CONTAINER};
                color: {COLOR_ON_SURFACE_VARIANT};
            }}
        """)
        self.send_btn.clicked.connect(self._on_send)
        input_lay.addWidget(self.send_btn)

        root.addWidget(input_bar)

        # Ctrl+Enter to send
        self.input.installEventFilter(self)

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                    mods & Qt.KeyboardModifier.ControlModifier:
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    # --- Public API --------------------------------------------------------

    def add_user_message(self, content: str) -> ChatMessage:
        """Append a user message row and auto-scroll."""
        row = UserRow(content)
        self._insert_row_before_stretch(row)
        self._scroll_to_bottom()
        return ChatMessage("user", content)

    def add_assistant_message(self, content: str,
                              sender: str = "assistant") -> ChatMessage:
        """Append an assistant message row and auto-scroll."""
        row = AssistantRow(content, sender=sender)
        self._insert_row_before_stretch(row)
        self._scroll_to_bottom()
        return ChatMessage("assistant", content)

    def add_tool_chip(self, tool_name: str, args_str: str,
                      result_str: str, success: bool,
                      duration: float) -> ChatMessage:
        """Append an expandable tool-call chip and auto-scroll."""
        row = ToolChipRow(tool_name, args_str, result_str, success, duration)
        self._insert_row_before_stretch(row)
        self._scroll_to_bottom()
        return ChatMessage("tool", tool_name=tool_name, tool_args=args_str,
                           tool_result=result_str, tool_success=success,
                           tool_duration=duration)

    def show_thinking(self, sender: str = "assistant") -> None:
        """Show the pulsing-dots thinking indicator."""
        if self._thinking_row is not None:
            self.hide_thinking()
        self._thinking_row = ThinkingRow(sender=sender)
        self._insert_row_before_stretch(self._thinking_row)
        self._scroll_to_bottom()

    def hide_thinking(self) -> None:
        """Remove the thinking indicator if present."""
        if self._thinking_row is not None:
            self._thinking_row.stop()
            idx = self._messages_layout.indexOf(self._thinking_row)
            if idx >= 0:
                self._messages_layout.takeAt(idx)
            self._thinking_row.deleteLater()
            self._thinking_row = None

    def restore_from_messages(self, messages: list[ChatMessage]) -> None:
        """Clear and rebuild the message list from saved messages."""
        self.clear_messages()
        for m in messages:
            if m.role == "user":
                self.add_user_message(m.content)
            elif m.role == "assistant":
                self.add_assistant_message(m.content)
            elif m.role == "tool":
                self.add_tool_chip(
                    m.tool_name, m.tool_args, m.tool_result,
                    m.tool_success, m.tool_duration,
                )

    def clear_messages(self) -> None:
        """Remove all message rows (but not the trailing stretch)."""
        self.hide_thinking()
        # Remove all items except the last (the stretch).
        while self._messages_layout.count() > 1:
            item = self._messages_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def set_input_enabled(self, enabled: bool) -> None:
        """Enable/disable the input + send button (e.g. while thinking)."""
        self.input.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)

    # --- Internal ----------------------------------------------------------

    def _insert_row_before_stretch(self, row: QWidget) -> None:
        """Insert a row before the trailing stretch item."""
        # The stretch is the last item (index count-1).
        stretch_idx = self._messages_layout.count() - 1
        self._messages_layout.insertWidget(stretch_idx, row)

    def _scroll_to_bottom(self) -> None:
        """Scroll the message list to the bottom."""
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.message_sent.emit(text)