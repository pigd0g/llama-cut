"""Stage 8 — Final Video Production UI.

The user reviews the approved storyboard, provides feedback, and triggers
the editing agent. The agent runs in a background thread with live tool
call logging. The user can iterate with feedback to refine the edit.
"""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
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

from ..context import ContextStore
from ..context_review import markdown_to_html
from ..storyboard import load_latest_storyboard
from ..theme import (
    COLOR_BORDER,
    COLOR_ON_SURFACE_VARIANT,
    COLOR_SURFACE,
    RADIUS_LG,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
)
from ..video_production import (
    VideoProductionSettings,
    clear_production,
    load_edit_plan,
    load_tool_log,
)
from ..workers.video_production_worker import VideoProductionWorker
from .context_review_page import _AutoTextBrowser


class VideoProductionPage(QWidget):
    """Stage 8 — produce the final video using an LLM editing agent."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._store: ContextStore | None = None
        self._worker: VideoProductionWorker | None = None
        self._storyboard_md: str = ""
        self._edit_plan = None       # EditPlan
        self._edit_plan_json: str = ""
        self._tool_log: list[dict] = []
        self._build()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        header = QHBoxLayout()
        title = QLabel("Final Video Production")
        title.setProperty("class", "headline-md")
        header.addWidget(title)
        header.addStretch()
        self.status_label = QLabel("")
        self.status_label.setProperty("class", "label-sm")
        header.addWidget(self.status_label)
        root.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(SPACING_MD)
        self.scroll.setWidget(self._content)
        root.addWidget(self.scroll, 1)

        root.addWidget(self._build_progress_block())

        footer = QHBoxLayout()
        footer.addStretch()
        self.back_btn = QPushButton("Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self._on_back)
        footer.addWidget(self.back_btn)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_btn.clicked.connect(self._on_reset)
        footer.addWidget(self.reset_btn)
        self.preview_btn = QPushButton("Render Preview")
        self.preview_btn.setProperty("class", "primary")
        self.preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_btn.clicked.connect(lambda: self._on_generate("preview"))
        footer.addWidget(self.preview_btn)
        self.final_btn = QPushButton("Render Final")
        self.final_btn.setProperty("class", "primary")
        self.final_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.final_btn.clicked.connect(lambda: self._on_generate("youtube_1080p"))
        footer.addWidget(self.final_btn)
        root.addLayout(footer)

    def _build_progress_block(self) -> QWidget:
        block = QFrame()
        block.setProperty("class", "card")
        lay = QVBoxLayout(block)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        self.progress_label = QLabel("")
        self.progress_label.setProperty("class", "label-md")
        lay.addWidget(self.progress_label)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(120)
        self.log_box.setProperty("class", "body-sm")
        lay.addWidget(self.log_box)
        block.setVisible(False)
        self._progress_block = block
        return block

    # --- Lifecycle ---------------------------------------------------------
    def on_enter(self) -> None:
        """Called when navigating to this stage."""
        if not self._state.working_folder:
            return
        self._store = ContextStore(Path(self._state.working_folder) / "context")
        self._storyboard_md = load_latest_storyboard(self._state.working_folder)
        self._edit_plan = load_edit_plan(self._state.working_folder)
        self._tool_log = []
        self._build_content()
        self._reset_button_states()
        QTimer.singleShot(50, self._sync_content_width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.scroll is not None and self._content is not None:
            viewport_w = self.scroll.viewport().width()
            if viewport_w > 0 and viewport_w != self._content.width():
                self._content.resize(viewport_w, self._content.height())
                self._sync_content_width()

    # --- Content building --------------------------------------------------
    def _build_content(self) -> None:
        """Clear and rebuild the scrolling content."""
        self.browser = None
        self.tool_log_browser = None
        self.edit_plan_browser = None

        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        has_storyboard = bool(self._storyboard_md.strip())

        if not has_storyboard:
            self._content_layout.addWidget(self._build_empty_state())
            self._content_layout.addStretch()
            return

        # --- Storyboard card (read-only) ---
        self._content_layout.addWidget(self._build_storyboard_card())

        # --- Feedback / prompt card ---
        self._content_layout.addWidget(self._build_feedback_card())

        # --- Tool log card (only if there are tool calls) ---
        if self._tool_log:
            self._content_layout.addWidget(self._build_tool_log_card())

        # --- Edit plan card (only if a plan exists) ---
        if self._edit_plan is not None:
            self._content_layout.addWidget(self._build_edit_plan_card())

        self._content_layout.addStretch()

    def _build_empty_state(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)
        lbl = QLabel("No storyboard found. Generate a storyboard in Stage 7 first.")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setProperty("class", "body-md")
        lbl.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        lay.addWidget(lbl)
        go_btn = QPushButton("Go to Storyboard")
        go_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        go_btn.clicked.connect(lambda: self._state.set_stage(7))
        lay.addWidget(go_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        return card

    def _build_storyboard_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Approved Storyboard")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        html = markdown_to_html(self._storyboard_md)
        self.browser = self._make_text_browser(html)
        lay.addWidget(self.browser)
        return card

    def _build_feedback_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Feedback")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        desc = QLabel(
            "Enter feedback to refine the edit, or leave empty for an "
            "initial render from the storyboard."
        )
        desc.setProperty("class", "body-sm")
        desc.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        self.feedback_input = QPlainTextEdit()
        self.feedback_input.setPlaceholderText(
            "e.g. Make the opening shorter.\n"
            "e.g. Use more of the kids talking.\n"
            "e.g. The ending is too abrupt."
        )
        _line_h = int(self.feedback_input.fontMetrics().lineSpacing())
        _h = 3 * _line_h + 2 * SPACING_MD + 8
        self.feedback_input.setFixedHeight(_h)
        self.feedback_input.setSizePolicy(
            self.feedback_input.sizePolicy().Policy.Expanding,
            self.feedback_input.sizePolicy().Policy.Fixed,
        )
        self.feedback_input.setProperty("class", "body-md")
        lay.addWidget(self.feedback_input)

        # Restore last feedback
        if self._state.video_production_settings.last_feedback:
            self.feedback_input.setPlainText(
                self._state.video_production_settings.last_feedback
            )

        return card

    def _build_tool_log_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Tool Execution Log")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        lines: list[str] = []
        for entry in self._tool_log:
            tool = entry.get("tool", "?")
            success = entry.get("success", False)
            dur = entry.get("duration_s", 0.0)
            args = entry.get("args", {})
            args_str = json.dumps(args) if isinstance(args, dict) else str(args)
            status = "OK" if success else "FAIL"
            lines.append(f"[{tool}] {status} ({dur:.1f}s) {args_str}")

        self.tool_log_browser = self._make_text_browser(
            "<pre>" + "\n".join(lines) + "</pre>"
            if lines else "<i>No tool calls yet.</i>"
        )
        lay.addWidget(self.tool_log_browser)
        return card

    def _build_edit_plan_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Edit Plan")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        # Status line (plan lifecycle: draft → executing → rendered → verified)
        if self._edit_plan is not None and self._edit_plan.status:
            status = self._edit_plan.status
        else:
            status = "draft"
        self._edit_plan_status_label = QLabel(f"Status: {status}")
        self._edit_plan_status_label.setProperty("class", "label-sm")
        self._edit_plan_status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        lay.addWidget(self._edit_plan_status_label)

        # Per-shot traceability table (rendered as HTML) — placed above the
        # raw JSON so the structured plan is easy to scan at a glance.
        if self._edit_plan is not None and self._edit_plan.timeline:
            self.traceability_browser = self._make_text_browser(
                _build_traceability_html(self._edit_plan)
            )
        else:
            self.traceability_browser = self._make_text_browser(
                "<i>No shots in the plan yet.</i>"
            )
        lay.addWidget(self.traceability_browser)

        # Raw JSON (kept for power users / debugging).
        if self._edit_plan is not None:
            plan_json = self._edit_plan.model_dump_json(indent=2)
        else:
            plan_json = "{}"
        self._edit_plan_json = plan_json

        self.edit_plan_browser = self._make_text_browser(
            "<pre>" + _escape_html(plan_json) + "</pre>"
        )
        lay.addWidget(self.edit_plan_browser)
        return card

    # --- Widget factory helpers --------------------------------------------
    def _make_text_browser(self, html: str) -> QTextBrowser:
        browser = _AutoTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setProperty("class", "body-sm")
        browser.setStyleSheet(
            f"QTextBrowser {{ background-color: {COLOR_SURFACE}; "
            f"border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {RADIUS_LG}px; "
            f"padding: {SPACING_MD}px; }}"
        )
        browser.setHtml(html)
        return browser

    # --- Sync content width + scrollbar -----------------------------------
    def _sync_content_width(self) -> None:
        if self.scroll is None or self._content is None:
            return
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        viewport_w = self.scroll.viewport().width()
        if viewport_w > 0:
            self._content.resize(viewport_w, self._content.height())
        from PyQt6 import sip
        if hasattr(self, "browser") and self.browser is not None and not sip.isdeleted(self.browser):
            self.browser._adjust_height()
        if hasattr(self, "tool_log_browser") and self.tool_log_browser is not None and not sip.isdeleted(self.tool_log_browser):
            self.tool_log_browser._adjust_height()
        if hasattr(self, "traceability_browser") and self.traceability_browser is not None and not sip.isdeleted(self.traceability_browser):
            self.traceability_browser._adjust_height()
        if hasattr(self, "edit_plan_browser") and self.edit_plan_browser is not None and not sip.isdeleted(self.edit_plan_browser):
            self.edit_plan_browser._adjust_height()
        min_h = self._content_layout.minimumSize().height()
        hint_h = self._content_layout.sizeHint().height()
        content_h = max(min_h, hint_h)
        if content_h > 0:
            self._content.setMinimumHeight(content_h)
            viewport_h = self.scroll.viewport().height()
            scroll_range = max(0, content_h - viewport_h)
            sb = self.scroll.verticalScrollBar()
            sb.setRange(0, scroll_range)
            sb.setPageStep(max(1, viewport_h))
            sb.setSingleStep(20)

    # --- Generate / Refine -------------------------------------------------
    def _is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _on_generate(self, preset: str) -> None:
        if self._is_busy():
            return
        if not self._state.working_folder or self._store is None:
            return
        from ..video_production import is_config_valid
        ok, msg = is_config_valid()
        if not ok:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Configuration missing", msg)
            return

        selected_videos = self._state.selected_videos
        if not selected_videos:
            self.status_label.setText("No videos selected.")
            self.status_label.setStyleSheet("color: #ef4444;")
            return

        feedback = self.feedback_input.toPlainText().strip()
        self._state.set_video_production_settings(
            VideoProductionSettings(last_feedback=feedback)
        )

        is_refinement = self._edit_plan is not None and bool(feedback)

        self._progress_block.setVisible(True)
        self.log_box.clear()
        self._set_buttons_busy(True)

        self._worker = VideoProductionWorker(
            feedback=feedback,
            existing_edit_plan_json=self._edit_plan_json if is_refinement else None,
            working_folder=self._state.working_folder,
            selected_videos=selected_videos,
            context_store=self._store,
            is_refinement=is_refinement,
            render_preset=preset,
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._on_log)
        self._worker.finished_success.connect(self._on_finished_success)
        self._worker.finished_error.connect(self._on_finished_error)
        self._worker.start()

    def _on_progress(self, msg: str) -> None:
        self.progress_label.setText(msg)

    def _on_log(self, msg: str) -> None:
        self.log_box.appendPlainText(msg)

    def _on_finished_success(self, edit_plan) -> None:
        self._progress_block.setVisible(False)
        self.status_label.setText("Video production complete.")
        self.status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        self._edit_plan = edit_plan
        self._edit_plan_json = edit_plan.model_dump_json(indent=2) if edit_plan else ""
        self._tool_log = load_tool_log(self._state.working_folder)
        self._build_content()
        self._reset_button_states()
        QTimer.singleShot(50, self._sync_content_width)

    def _on_finished_error(self, msg: str) -> None:
        self._progress_block.setVisible(False)
        self.status_label.setText(f"Error: {msg}")
        self.status_label.setStyleSheet("color: #ef4444;")
        self._reset_button_states()

    def _set_buttons_busy(self, busy: bool) -> None:
        self.preview_btn.setEnabled(not busy)
        self.final_btn.setEnabled(not busy)
        self.back_btn.setEnabled(not busy)
        self.reset_btn.setEnabled(not busy)

    def _reset_button_states(self) -> None:
        has_storyboard = bool(self._storyboard_md.strip())
        self.preview_btn.setEnabled(has_storyboard)
        self.final_btn.setEnabled(has_storyboard)
        self.back_btn.setEnabled(True)
        self.reset_btn.setEnabled(has_storyboard and not self._is_busy())

    # --- Reset -------------------------------------------------------------
    def _on_reset(self) -> None:
        if self._is_busy():
            return
        if not self._state.working_folder:
            return
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "Reset Production",
            "This will permanently delete all video production artefacts "
            "(clips, output, edit plan, tool log). This cannot be undone.\n\n"
            "Are you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_production(self._state.working_folder)
        self._edit_plan = None
        self._edit_plan_json = ""
        self._tool_log = []
        self._state.set_video_production_settings(VideoProductionSettings(last_feedback=""))
        self.status_label.setText("Production reset.")
        self.status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        self._build_content()
        self._reset_button_states()
        QTimer.singleShot(50, self._sync_content_width)

    # --- Navigation --------------------------------------------------------
    def _on_back(self) -> None:
        self._state.set_stage(7)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_traceability_html(edit_plan) -> str:
    """Render the edit plan's timeline as a per-shot traceability table.

    Columns: Shot | Scene | Source | Start–End | Clip | Purpose.
    Placed above the raw JSON in the Edit Plan card so the structured plan
    is easy to scan at a glance.
    """
    rows = []
    for item in edit_plan.timeline:
        start_end = (
            f"{item.source_start:.1f}–{item.source_end:.1f}s"
            if item.source_start is not None and item.source_end is not None
            else "—"
        )
        scene = _escape_html(item.storyboard_scene or item.storyboard_shot or "")
        purpose = _escape_html(item.purpose or "")
        clip = _escape_html(item.intermediate_clip or "")
        src = _escape_html(item.source or "")
        shot_id = _escape_html(item.id or "")
        rows.append(
            "<tr>"
            f"<td><b>{shot_id}</b></td>"
            f"<td>{scene}</td>"
            f"<td>{src}</td>"
            f"<td>{start_end}</td>"
            f"<td>{clip}</td>"
            f"<td>{purpose}</td>"
            "</tr>"
        )
    if not rows:
        return "<i>No shots in the plan.</i>"
    header = (
        "<tr>"
        "<th align='left'>Shot</th>"
        "<th align='left'>Scene</th>"
        "<th align='left'>Source</th>"
        "<th align='left'>Start–End</th>"
        "<th align='left'>Clip</th>"
        "<th align='left'>Purpose</th>"
        "</tr>"
    )
    style = (
        "table { border-collapse: collapse; width: 100%; }"
        "th, td { padding: 4px 8px; border-bottom: 1px solid #2a3344; "
        "vertical-align: top; text-align: left; }"
        "th { font-weight: 600; }"
    )
    return (
        f"<style>{style}</style>"
        f"<p><b>{len(rows)} shot(s)</b> · status: {_escape_html(edit_plan.status or 'draft')}</p>"
        f"<table>{header}{''.join(rows)}</table>"
    )