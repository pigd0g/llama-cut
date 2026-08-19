from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QPixmap
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

from ..context import ContextStore, ContextType
from ..context_review import (
    build_export_markdown,
    load_assembled,
    markdown_to_html,
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


# Debounce window for autosave after the last keystroke (matches MarkdownEditor).
AUTOSAVE_DEBOUNCE_MS = 1000

THUMB_W = 192
THUMB_H = 108


class ContextReviewPage(QWidget):
    """Stage 6 — review and edit all gathered context as a single document.

    Project context appears once at the top. Each selected video follows as a
    card with its thumbnail, then Video Context, Transcription, and Frame
    Analysis sections. Frame Analysis sections render inline frame images.

    Edits are debounced-autosaved back to the individual .md files via the
    ContextStore, parsing the assembled document by heading on save.
    """

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._store: ContextStore | None = None
        self._video_stems: list[tuple[str, str]] = []  # (stem, name) in order
        self._frame_paths_by_filename: dict[str, str] = {}
        self._editors: dict[str, QPlainTextEdit] = {}  # key -> editor
        self._edit_toggles: dict[str, QPushButton] = {}  # key -> toggle button
        self._dirty: set[str] = set()  # keys with pending changes
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
        title = QLabel("Context Review")
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

        footer = QHBoxLayout()
        footer.addStretch()
        self.back_btn = QPushButton("Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self._on_back)
        footer.addWidget(self.back_btn)
        self.export_btn = QPushButton("Export Report")
        self.export_btn.setProperty("class", "primary")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._on_export)
        footer.addWidget(self.export_btn)
        root.addLayout(footer)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep the content widget's width matched to the scroll area viewport
        # so auto-sizing widgets re-wrap on window resize.
        if self.scroll is not None and self._content is not None:
            viewport_w = self.scroll.viewport().width()
            if viewport_w > 0 and viewport_w != self._content.width():
                self._content.resize(viewport_w, self._content.height())
                self._sync_content_width()

    # --- Lifecycle ---------------------------------------------------------
    def on_enter(self) -> None:
        """Called when navigating to this stage. Rebuilds the document."""
        if not self._state.working_folder:
            return
        self._store = ContextStore(Path(self._state.working_folder) / "context")
        # Save any pending edits from a previous visit.
        self._flush_save()
        self._build_frame_paths()
        self._build_document()
        # Defer the width/height sync until the page is actually visible and
        # the scroll area's viewport has a real width. Use a longer delay to
        # ensure the QStackedWidget has switched to this page.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(50, self._sync_content_width)

    def _build_frame_paths(self) -> None:
        """Build {frame_filename: abs_path} from state.frames for image rendering."""
        self._frame_paths_by_filename = {}
        for f in self._state.frames:
            if f.filename and f.path and Path(f.path).exists():
                self._frame_paths_by_filename[f.filename] = f.path

    def _build_document(self) -> None:
        """Clear and rebuild the scrolling document from the ContextStore."""
        # Clear existing widgets
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._editors.clear()
        self._edit_toggles.clear()
        self._dirty.clear()

        doc = load_assembled(self._state, self._store)
        self._video_stems = [(v.stem, v.name) for v in doc.videos]

        # Project context card
        self._content_layout.addWidget(self._build_project_card(doc))

        # Per-video cards
        for v in doc.videos:
            self._content_layout.addWidget(self._build_video_card(v))

        # No stretch — the layout's minimum size drives the content height so
        # the outer QScrollArea shows a scrollbar when content exceeds viewport.
        # Defer width sync so the auto-sizing widgets settle at their correct
        # heights after the content widget gets a real width from the scroll area.

    def _sync_content_width(self) -> None:
        """Match the content widget's width to the scroll area's viewport and
        trigger all auto-sizing widgets to re-measure."""
        if self.scroll is None or self._content is None:
            return
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        viewport_w = self.scroll.viewport().width()
        if viewport_w > 0:
            self._content.resize(viewport_w, self._content.height())
        # Trigger each auto-sized widget to re-measure now that width is real.
        for editor in self._editors.values():
            card = editor.parentWidget()
            if card is None:
                continue
            browser = card.findChild(QTextBrowser, "rendered_view")
            if browser is not None:
                browser._adjust_height()
            editor._adjust_height()
        # Compute the total content height from the layout and set the
        # content widget's minimum height so the QScrollArea (with
        # setWidgetResizable=True) shows a scrollbar when content exceeds viewport.
        min_h = self._content_layout.minimumSize().height()
        hint_h = self._content_layout.sizeHint().height()
        content_h = max(min_h, hint_h)
        if content_h > 0:
            self._content.setMinimumHeight(content_h)
            # Manually set the scrollbar range. With setWidgetResizable(True),
            # Qt does not always update the scrollbar range when the widget's
            # minimum height changes programmatically, so we force it here.
            viewport_h = self.scroll.viewport().height()
            scroll_range = max(0, content_h - viewport_h)
            sb = self.scroll.verticalScrollBar()
            sb.setRange(0, scroll_range)
            sb.setPageStep(max(1, viewport_h))
            sb.setSingleStep(20)

    def _build_project_card(self, doc) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        # Header row
        hdr = QHBoxLayout()
        hdr.setSpacing(SPACING_SM)
        title = QLabel("Project Context")
        title.setProperty("class", "headline-sm")
        hdr.addWidget(title)
        hdr.addStretch()
        edit_toggle = QPushButton("Edit")
        edit_toggle.setCheckable(True)
        edit_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_toggle.clicked.connect(lambda checked: self._on_edit_toggle("project", checked))
        self._edit_toggles["project"] = edit_toggle
        hdr.addWidget(edit_toggle)
        lay.addLayout(hdr)

        # Read-only rendered view
        rendered = self._render_project_html(doc.project_context)
        browser = self._make_text_browser(rendered)
        lay.addWidget(browser)

        # Editable plain-text editor (hidden by default)
        editor = self._make_plain_editor(doc.project_context)
        editor.textChanged.connect(lambda: self._on_text_changed("project"))
        editor.setVisible(False)
        lay.addWidget(editor)
        self._editors["project"] = editor

        # Tag the browser so the toggle can swap visibility
        browser.setObjectName("rendered_view")
        editor.setObjectName("edit_view")
        return card

    def _build_video_card(self, v) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        lay.setSpacing(SPACING_SM)

        # Header row: thumbnail + name + edit toggle
        hdr = QHBoxLayout()
        hdr.setSpacing(SPACING_SM)
        thumb = QLabel()
        thumb.setFixedSize(THUMB_W // 2, THUMB_H // 2)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(
            f"background-color: {COLOR_SURFACE_CONTAINER}; "
            f"border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {RADIUS_LG}px;"
        )
        if v.thumbnail_path and Path(v.thumbnail_path).exists():
            pix = QPixmap(v.thumbnail_path)
            if not pix.isNull():
                scaled = pix.scaled(
                    THUMB_W // 2, THUMB_H // 2,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                thumb.setPixmap(scaled)
        hdr.addWidget(thumb)

        name = QLabel(v.name)
        name.setStyleSheet(f"font-weight: 600; color: {COLOR_ON_SURFACE};")
        hdr.addWidget(name)
        hdr.addStretch()
        edit_toggle = QPushButton("Edit")
        edit_toggle.setCheckable(True)
        edit_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_toggle.clicked.connect(
            lambda checked, stem=v.stem: self._on_edit_toggle(stem, checked)
        )
        self._edit_toggles[v.stem] = edit_toggle
        hdr.addWidget(edit_toggle)
        lay.addLayout(hdr)

        # Build the assembled markdown for this video's 3 sections, then render.
        video_md = self._assemble_video_markdown(v)
        rendered = markdown_to_html(video_md, self._frame_paths_by_filename)
        browser = self._make_text_browser(rendered)
        lay.addWidget(browser)

        # Editable plain-text editor (hidden by default) — contains the raw
        # markdown for the 3 sections.
        editor = self._make_plain_editor(video_md)
        editor.textChanged.connect(lambda: self._on_text_changed(v.stem))
        editor.setVisible(False)
        lay.addWidget(editor)
        self._editors[v.stem] = editor

        browser.setObjectName("rendered_view")
        editor.setObjectName("edit_view")
        return card

    # --- Widget factory helpers --------------------------------------------
    def _make_text_browser(self, html: str) -> QTextBrowser:
        """Build a QTextBrowser sized to its content (no internal scrollbar)."""
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
        """Build a QPlainTextEdit sized to its content (no internal scrollbar)."""
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

    def _assemble_video_markdown(self, v) -> str:
        """Build the markdown for one video's 3 sections (for rendering + editing)."""
        from ..context_review import _strip_leading_heading
        parts: list[str] = []
        parts.append("## Video Context")
        parts.append("")
        parts.append(_strip_leading_heading(v.video_context, "# Video Context").strip()
                     or "_No video context provided._")
        parts.append("")
        parts.append("## Transcription")
        parts.append("")
        parts.append(_strip_leading_heading(v.transcription, "# Transcription").strip()
                     or "_Not yet generated._")
        parts.append("")
        parts.append("## Frame Analysis")
        parts.append("")
        parts.append(_strip_leading_heading(v.frame_analysis, "# Frame Analysis").strip()
                     or "_Not yet generated._")
        parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    def _render_project_html(self, project_ctx: str) -> str:
        from ..context_review import _strip_leading_heading
        body = _strip_leading_heading(project_ctx, "# Project Context")
        md = f"# Project Context\n\n{body.strip() or '_No project context provided._'}"
        return markdown_to_html(md, self._frame_paths_by_filename)

    # --- Edit toggle -------------------------------------------------------
    def _on_edit_toggle(self, key: str, checked: bool) -> None:
        """Swap between rendered view (QTextBrowser) and editor (QPlainTextEdit)."""
        # Find the card containing this key's widgets.
        editor = self._editors.get(key)
        toggle = self._edit_toggles.get(key)
        if editor is None:
            return
        # The rendered browser is the sibling with objectName "rendered_view".
        parent = editor.parentWidget()
        if parent is None:
            return
        browser = parent.findChild(QTextBrowser, "rendered_view")
        if checked:
            # Switch to edit mode: hide browser, show editor.
            if browser is not None:
                browser.setVisible(False)
            editor.setVisible(True)
            editor.setFocus()
            if toggle is not None:
                toggle.setText("Done")
        else:
            # Switch back to rendered view: flush save, hide editor, show browser.
            self._flush_key(key)
            editor.setVisible(False)
            if browser is not None:
                # Re-render from the (now saved) content.
                if key == "project":
                    doc = load_assembled(self._state, self._store)
                    browser.setHtml(self._render_project_html(doc.project_context))
                else:
                    # Rebuild the video markdown from the saved sections.
                    v = next((vv for vv in load_assembled(self._state, self._store).videos
                              if vv.stem == key), None)
                    if v is not None:
                        browser.setHtml(
                            markdown_to_html(
                                self._assemble_video_markdown(v),
                                self._frame_paths_by_filename,
                            )
                        )
                browser.setVisible(True)
            if toggle is not None:
                toggle.setText("Edit")

    # --- Debounced autosave ------------------------------------------------
    def _on_text_changed(self, key: str) -> None:
        self._dirty.add(key)
        self._timer.start(AUTOSAVE_DEBOUNCE_MS)

    def _flush_save(self) -> None:
        """Persist all dirty sections to disk."""
        if not self._dirty or self._store is None:
            return
        for key in list(self._dirty):
            self._flush_key(key)

    def _flush_key(self, key: str) -> None:
        """Persist a single section to disk, parsing the editor content."""
        if self._store is None or key not in self._dirty:
            return
        editor = self._editors.get(key)
        if editor is None:
            self._dirty.discard(key)
            return
        content = editor.toPlainText()
        if key == "project":
            self._store.save(None, ContextType.PROJECT, content)
        else:
            # Parse the video's 3 sections from the editor content.
            sections = self._parse_video_sections(content)
            self._store.save(key, ContextType.VIDEO, sections["video"])
            self._store.save(key, ContextType.TRANSCRIPTION, sections["transcription"])
            self._store.save(key, ContextType.FRAME_ANALYSIS, sections["frame_analysis"])
        self._dirty.discard(key)

    def _parse_video_sections(self, md: str) -> dict:
        """Parse a video editor's markdown back into its 3 sections."""
        from ..context_review import _split_video_sections
        return _split_video_sections(md)

    # --- Navigation --------------------------------------------------------
    def _on_back(self) -> None:
        self._flush_save()
        self._state.set_stage(5)

    # --- Export ------------------------------------------------------------
    def _on_export(self) -> None:
        """Export the assembled document to <working_folder>/video_context_report.md."""
        if self._store is None or not self._state.working_folder:
            return
        self._flush_save()
        # Reload after the flush so the export reflects the latest edits.
        doc = load_assembled(self._state, self._store)
        # Rebuild frame paths in case frames changed.
        self._build_frame_paths()
        export_md = build_export_markdown(doc, self._frame_paths_by_filename)
        out_path = Path(self._state.working_folder) / "video_context_report.md"
        try:
            out_path.write_text(export_md, encoding="utf-8")
            self.status_label.setText(f"Exported to {out_path}")
            self.status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        except OSError as e:
            self.status_label.setText(f"Export failed: {e}")
            self.status_label.setStyleSheet("color: #ef4444;")


# --- Auto-sizing widgets ----------------------------------------------------

class _AutoTextBrowser(QTextBrowser):
    """A QTextBrowser that sizes itself to its content and hides scrollbars.

    The widget's height tracks the document height so the outer QScrollArea
    is the only scrollbar the user sees.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().documentLayout().documentSizeChanged.connect(
            self._adjust_height
        )

    def setHtml(self, html: str) -> None:
        super().setHtml(html)
        # Defer the height adjustment so the widget has a real width from the
        # layout before we measure the document (text wrapping depends on width).
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._adjust_height)

    def _adjust_height(self, *_args) -> None:
        # Only adjust once the widget has a real width from the layout.
        if self.width() <= 0:
            return
        # Set the document's text width to match the widget's content width
        # so the layout re-wraps to the correct width before we measure.
        # QTextDocument.textWidth defaults to the viewport width, but may be
        # stale from when the widget had width=0.
        content_width = self.width() - 2 * SPACING_MD - 4  # padding + border
        if content_width > 0:
            self.document().setTextWidth(content_width)
        doc_height = int(self.document().size().height())
        # Add a small margin so the last line isn't clipped.
        self.setFixedHeight(max(doc_height + 4, 40))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Re-evaluate on width changes (text reflows).
        self._adjust_height()


class _AutoPlainEdit(QPlainTextEdit):
    """A QPlainTextEdit that sizes itself to its content and hides scrollbars.

    The widget's height tracks the number of lines so the outer QScrollArea
    is the only scrollbar the user sees.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.textChanged.connect(self._adjust_height)

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._adjust_height)

    def _adjust_height(self, *_args) -> None:
        if self.width() <= 0:
            return
        # Compute the height needed to show all lines without scrolling.
        lines = max(self.blockCount(), 1)
        line_height = int(self.fontMetrics().lineSpacing())
        # 2x SPACING_MD padding (top + bottom) + a few px of slack.
        height = lines * line_height + 2 * SPACING_MD + 8
        self.setFixedHeight(max(height, 40))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adjust_height()