from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
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
from ..storyboard import (
    StoryboardSettings,
    clear_storyboard,
    export_storyboard,
    is_config_valid,
    load_history,
    load_latest_storyboard,
    save_history,
    save_latest_storyboard,
)
from ..theme import (
    COLOR_BORDER,
    COLOR_ON_SURFACE,
    COLOR_ON_SURFACE_VARIANT,
    COLOR_SURFACE,
    COLOR_SURFACE_CONTAINER,
    RADIUS_LG,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
)
from ..workers.storyboard_worker import StoryboardWorker
from .context_review_page import _AutoPlainEdit, _AutoTextBrowser


AUTOSAVE_DEBOUNCE_MS = 1000


class StoryboardPage(QWidget):
    """Stage 7 — generate, review, and refine a video storyboard.

    The user enters a creative brief, which is sent to the Ollama workflow
    model along with all project/video context + ffprobe metadata. The
    resulting storyboard is displayed in a scrollable, editable review
    interface. The user can iterate with additional prompts to refine the
    storyboard. All versions are persisted to storyboard/history.json.
    """

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._store: ContextStore | None = None
        self._worker: StoryboardWorker | None = None
        self._history = None          # StoryboardHistory
        self._storyboard_md: str = ""  # latest storyboard markdown
        self._dirty: bool = False      # pending manual edit
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush_save)
        self._build()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        header = QHBoxLayout()
        title = QLabel("Storyboard")
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
        self.export_btn = QPushButton("Export Storyboard")
        self.export_btn.setProperty("class", "primary")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._on_export)
        footer.addWidget(self.export_btn)
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
        self._flush_save()
        self._history = load_history(self._state.working_folder)
        self._storyboard_md = load_latest_storyboard(self._state.working_folder)
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
        """Clear and rebuild the scrolling content from history + latest storyboard."""
        # Clear references to widgets that are about to be deleted so
        # deferred calls (e.g. _sync_content_width) don't touch dead C++ objects.
        self.browser = None
        self.editor = None
        self.edit_toggle = None

        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        has_storyboard = bool(self._storyboard_md.strip())
        history_versions = self._history.versions if self._history else []

        # --- Brief / Iteration Prompt card ---
        self._content_layout.addWidget(self._build_prompt_card(has_storyboard))

        # --- History card (only if there are versions) ---
        if history_versions:
            self._content_layout.addWidget(self._build_history_card(history_versions))

        # --- Storyboard card (hidden until a storyboard exists) ---
        if has_storyboard:
            self._content_layout.addWidget(self._build_storyboard_card())

        # Trailing stretch keeps cards top-aligned when the content is shorter
        # than the scroll area viewport (e.g. empty state with only the prompt
        # card). When content exceeds the viewport the stretch has zero size
        # and the scrollbar range is unaffected.
        self._content_layout.addStretch()

    def _build_prompt_card(self, has_storyboard: bool) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Creative Brief" if not has_storyboard else "Refinement Prompt")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setPlaceholderText(
            "Describe the video you want to create...\n\n"
            "e.g. Make a short 2-3 minute vlog for YouTube about our family holiday."
        )
        # Fixed height: 4 lines. Use the font metrics to compute the exact
        # pixel height so it doesn't scale with the window.
        _line_h = int(self.prompt_input.fontMetrics().lineSpacing())
        _prompt_h = 4 * _line_h + 2 * SPACING_MD + 8  # 4 lines + padding + slack
        self.prompt_input.setFixedHeight(_prompt_h)
        self.prompt_input.setSizePolicy(
            self.prompt_input.sizePolicy().Policy.Fixed,
            self.prompt_input.sizePolicy().Policy.Fixed,
        )
        self.prompt_input.setProperty("class", "body-md")
        lay.addWidget(self.prompt_input)

        # Restore last brief from state settings
        if self._state.storyboard_settings.last_brief:
            self.prompt_input.setPlainText(self._state.storyboard_settings.last_brief)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        if has_storyboard:
            self.generate_btn = QPushButton("Refine")
        else:
            self.generate_btn = QPushButton("Generate Storyboard")
        self.generate_btn.setProperty("class", "primary")
        self.generate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.generate_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self.generate_btn)
        lay.addLayout(btn_row)
        return card

    def _build_history_card(self, versions: list) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        title = QLabel("Iteration History")
        title.setProperty("class", "headline-sm")
        lay.addWidget(title)

        for v in reversed(versions):
            entry = QFrame()
            entry.setStyleSheet(
                f"QFrame {{ background-color: {COLOR_SURFACE_CONTAINER}; "
                f"border: 1px solid {COLOR_BORDER}; "
                f"border-radius: {RADIUS_LG}px; "
                f"padding: {SPACING_SM}px; }}"
            )
            elay = QVBoxLayout(entry)
            elay.setContentsMargins(SPACING_SM, SPACING_SM, SPACING_SM, SPACING_SM)
            elay.setSpacing(2)
            header_label = QLabel(
                f"<b>v{v.version}</b> — {v.timestamp} ({v.model})"
                + (" [edited]" if v.updated else "")
            )
            header_label.setStyleSheet(f"color: {COLOR_ON_SURFACE};")
            header_label.setProperty("class", "label-md")
            elay.addWidget(header_label)
            prompt_label = QLabel(f"<i>{v.prompt}</i>")
            prompt_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
            prompt_label.setProperty("class", "body-sm")
            prompt_label.setWordWrap(True)
            elay.addWidget(prompt_label)
            lay.addWidget(entry)
        return card

    def _build_storyboard_card(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        hdr = QHBoxLayout()
        hdr.setSpacing(SPACING_SM)
        title = QLabel("Storyboard")
        title.setProperty("class", "headline-sm")
        hdr.addWidget(title)
        hdr.addStretch()
        self.edit_toggle = QPushButton("Edit")
        self.edit_toggle.setCheckable(True)
        self.edit_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_toggle.clicked.connect(self._on_edit_toggle)
        self.edit_toggle.setEnabled(bool(self._storyboard_md.strip()))
        hdr.addWidget(self.edit_toggle)
        lay.addLayout(hdr)

        # Rendered view
        html = markdown_to_html(self._storyboard_md) if self._storyboard_md.strip() else ""
        self.browser = self._make_text_browser(html or "<p><i>No storyboard generated yet. Enter a creative brief above and click Generate.</i></p>")
        self.browser.setObjectName("rendered_view")
        lay.addWidget(self.browser)

        # Editable plain-text editor (hidden by default)
        self.editor = self._make_plain_editor(self._storyboard_md)
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.setObjectName("edit_view")
        self.editor.setVisible(False)
        lay.addWidget(self.editor)
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

    def _make_plain_editor(self, text: str) -> QPlainTextEdit:
        editor = _AutoPlainEdit()
        mono = QFont("Cascadia Code", 10)
        if not mono.exactMatch():
            mono = QFont("Consolas", 10)
        if not mono.exactMatch():
            mono = QFont("Courier New", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        editor.setFont(mono)
        editor.setPlainText(text)
        editor.setProperty("class", "body-sm")
        editor.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {COLOR_SURFACE}; "
            f"border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {RADIUS_LG}px; "
            f"padding: {SPACING_MD}px; }}"
        )
        return editor

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
        # Trigger auto-sizing widgets to re-measure. Guard against deleted
        # C++ objects (can happen if _build_content ran between the deferred
        # timer firing and now, e.g. after a reset).
        from PyQt6 import sip
        if hasattr(self, "browser") and self.browser is not None and not sip.isdeleted(self.browser):
            self.browser._adjust_height()
        if hasattr(self, "editor") and self.editor is not None and not sip.isdeleted(self.editor):
            self.editor._adjust_height()
        # Compute content height and set the scrollbar range.
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

    def _on_generate(self) -> None:
        if self._is_busy():
            return
        prompt = self.prompt_input.toPlainText().strip()
        if not prompt:
            self.status_label.setText("Enter a prompt first.")
            self.status_label.setStyleSheet("color: #ef4444;")
            return
        if not self._state.working_folder or self._store is None:
            return
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

        # Save the prompt as the last brief.
        self._state.set_storyboard_settings(StoryboardSettings(last_brief=prompt))

        has_storyboard = bool(self._storyboard_md.strip())
        existing = self._storyboard_md if has_storyboard else None

        self._progress_block.setVisible(True)
        self.log_box.clear()
        self.generate_btn.setEnabled(False)
        self.back_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.reset_btn.setEnabled(False)
        if hasattr(self, "edit_toggle") and self.edit_toggle is not None:
            self.edit_toggle.setEnabled(False)

        self._worker = StoryboardWorker(
            prompt=prompt,
            existing_storyboard=existing,
            working_folder=self._state.working_folder,
            selected_videos=selected_videos,
            context_store=self._store,
            is_refinement=has_storyboard,
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

    def _on_finished_success(self, version) -> None:
        self._progress_block.setVisible(False)
        self.status_label.setText(f"Storyboard v{version.version} generated.")
        self.status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        # Reload history + latest storyboard, rebuild content.
        self._history = load_history(self._state.working_folder)
        self._storyboard_md = load_latest_storyboard(self._state.working_folder)
        self._build_content()
        self._reset_button_states()
        QTimer.singleShot(50, self._sync_content_width)

    def _on_finished_error(self, msg: str) -> None:
        self._progress_block.setVisible(False)
        self.status_label.setText(f"Error: {msg}")
        self.status_label.setStyleSheet("color: #ef4444;")
        self._reset_button_states()

    def _reset_button_states(self) -> None:
        has_storyboard = bool(self._storyboard_md.strip())
        self.generate_btn.setEnabled(True)
        self.back_btn.setEnabled(True)
        self.export_btn.setEnabled(has_storyboard)
        self.reset_btn.setEnabled(has_storyboard)
        # edit_toggle may be None if the storyboard card doesn't exist yet
        # (e.g. after reset when no storyboard is present).
        if hasattr(self, "edit_toggle") and self.edit_toggle is not None:
            self.edit_toggle.setEnabled(has_storyboard)
        # Update the generate button label.
        if has_storyboard:
            self.generate_btn.setText("Refine")
        else:
            self.generate_btn.setText("Generate Storyboard")

    # --- Reset -------------------------------------------------------------
    def _on_reset(self) -> None:
        """Delete all storyboard artefacts and return to a clean slate."""
        if self._is_busy():
            return
        if not self._state.working_folder:
            return
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "Reset Storyboard",
            "This will permanently delete all storyboard versions, history, "
            "and exports. This action cannot be undone.\n\n"
            "Are you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Cancel any pending edit save.
        self._dirty = False
        self._timer.stop()
        # Delete the storyboard directory entirely.
        clear_storyboard(self._state.working_folder)
        # Clear in-memory state.
        self._history = load_history(self._state.working_folder)
        self._storyboard_md = ""
        # Clear the persisted last brief as well so the prompt input starts fresh.
        self._state.set_storyboard_settings(StoryboardSettings(last_brief=""))
        self.status_label.setText("Storyboard reset.")
        self.status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        self._build_content()
        self._reset_button_states()
        QTimer.singleShot(50, self._sync_content_width)

    # --- Edit toggle -------------------------------------------------------
    def _on_edit_toggle(self, checked: bool) -> None:
        if checked:
            self.browser.setVisible(False)
            self.editor.setVisible(True)
            self.editor.setFocus()
            self.edit_toggle.setText("Done")
        else:
            # Flush save, re-render.
            self._flush_save()
            self.editor.setVisible(False)
            html = markdown_to_html(self._storyboard_md) if self._storyboard_md.strip() else ""
            self.browser.setHtml(html or "<p><i>No storyboard generated yet.</i></p>")
            self.browser.setVisible(True)
            self.edit_toggle.setText("Edit")
            QTimer.singleShot(50, self._sync_content_width)

    # --- Debounced autosave ------------------------------------------------
    def _on_text_changed(self) -> None:
        self._dirty = True
        self._timer.start(AUTOSAVE_DEBOUNCE_MS)

    def _flush_save(self) -> None:
        """Persist the edited storyboard to disk (latest version in-place)."""
        if not self._dirty or not self._state.working_folder:
            return
        self._dirty = False
        storyboard_md = self.editor.toPlainText()
        self._storyboard_md = storyboard_md
        save_latest_storyboard(self._state.working_folder, storyboard_md)
        # Update the latest version in history (in-place).
        history = load_history(self._state.working_folder)
        history.update_latest(storyboard_md)
        save_history(self._state.working_folder, history)
        self._history = history

    # --- Navigation --------------------------------------------------------
    def _on_back(self) -> None:
        self._flush_save()
        self._state.set_stage(6)

    # --- Export ------------------------------------------------------------
    def _on_export(self) -> None:
        """Export the latest storyboard to storyboard/storyboard_export.md."""
        if not self._state.working_folder:
            return
        self._flush_save()
        storyboard_md = self._storyboard_md or load_latest_storyboard(self._state.working_folder)
        if not storyboard_md.strip():
            self.status_label.setText("No storyboard to export.")
            self.status_label.setStyleSheet("color: #ef4444;")
            return
        try:
            p = export_storyboard(self._state.working_folder, storyboard_md)
            self.status_label.setText(f"Exported to {p}")
            self.status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        except OSError as e:
            self.status_label.setText(f"Export failed: {e}")
            self.status_label.setStyleSheet("color: #ef4444;")