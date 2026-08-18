from __future__ import annotations

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..context import ContextDoc, ContextSource
from ..theme import (
    COLOR_BORDER,
    COLOR_ON_SURFACE,
    COLOR_PRIMARY_CONTAINER,
    COLOR_SURFACE,
    COLOR_WARNING,
    COLOR_WARNING_BG,
    RADIUS_FULL,
    RADIUS_LG,
    SPACING_MD,
    SPACING_SM,
)


SLOT_TITLES = {
    "project": "Project Context",
    "video": "Video Context",
    "frame_analysis": "Frame Analysis",
    "transcription": "Transcription",
}

PLACEHOLDER_TEXT = {
    "frame_analysis": (
        "# Frame Analysis\n\n"
        "_Not yet generated — will be populated in a later phase._"
    ),
    "transcription": (
        "# Transcription\n\n"
        "_Not yet generated — will be populated in a later phase._"
    ),
    "project": (
        "# Project Context\n\n"
        "Describe the project's purpose, audience, background, terminology, "
        "and general analysis instructions here.\n"
    ),
    "video": (
        "# Video Context\n\n"
        "Describe this video's purpose, contents, notable moments, people, "
        "locations, and the questions the analysis should answer.\n"
    ),
}

# Debounce window for autosave after the last keystroke.
AUTOSAVE_DEBOUNCE_MS = 1000


class MarkdownEditor(QWidget):
    """Reusable single-slot Markdown editor with debounced autosave.

    User-authored slots (Project, Video) are always editable.
    Programmatic slots (Frame Analysis, Transcription) are read-only until
    they have been generated; the ContextPage disables their tabs entirely
    until then, so this editor is only constructed for them once content
    exists on disk.

    Changes are saved automatically:
      - debounced AUTOSAVE_DEBOUNCE_MS after the last keystroke
      - immediately on focus loss
      - immediately on save_now() (called by the page on navigation)
    """

    save_requested = pyqtSignal(str)  # emits new content on save

    def __init__(self, store, video_stem, ctype, parent=None):
        super().__init__(parent)
        self._store = store
        self._video_stem = video_stem
        self._ctype = ctype
        self._doc: ContextDoc | None = None
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush_save)
        self._build()
        self.reload()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING_SM)

        # Header row
        header = QHBoxLayout()
        header.setSpacing(SPACING_SM)
        self.title_label = QLabel(SLOT_TITLES.get(self._ctype.value, self._ctype.value))
        self.title_label.setProperty("class", "headline-sm")
        header.addWidget(self.title_label)
        header.addSpacing(SPACING_SM)

        self.source_badge = QLabel()
        self.source_badge.setFixedHeight(20)
        header.addWidget(self.source_badge)
        header.addStretch()

        self.meta_label = QLabel("")
        self.meta_label.setProperty("class", "label-sm")
        header.addWidget(self.meta_label)
        layout.addLayout(header)

        # Editor
        self.editor = QPlainTextEdit()
        # monospace editor
        mono = QFont("Cascadia Code", 10)
        if not mono.exactMatch():
            mono = QFont("Consolas", 10)
        if not mono.exactMatch():
            mono = QFont("Courier New", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.editor.setFont(mono)
        self.editor.setProperty("class", "body-sm")
        # override the QSS padding for an editor look
        self.editor.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {COLOR_SURFACE}; "
            f"border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {RADIUS_LG}px; "
            f"padding: {SPACING_MD}px; }}"
        )
        self.editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.editor, 1)

    # --- Lifecycle ---------------------------------------------------------
    def reload(self) -> None:
        """Reload from disk and reset mode based on source/existence."""
        self._doc = self._store.get(self._video_stem, self._ctype)
        if self._doc is None:
            file_path = self._store._file_for(self._video_stem, self._ctype)
            from ..context import DEFAULT_SOURCE
            self._doc = ContextDoc(
                type=self._ctype,
                source=DEFAULT_SOURCE[self._ctype],
                file_path=file_path,
                created="", updated="", content="",
            )
        # load content from file or placeholder
        if self._doc.exists():
            content = self._doc.content
        elif self._doc.source is ContextSource.USER:
            content = ""
        else:
            content = PLACEHOLDER_TEXT.get(self._ctype.value, "")

        self._cancel_pending_save()
        self.editor.blockSignals(True)
        self.editor.setPlainText(content)
        self.editor.blockSignals(False)
        self._dirty = False
        self._refresh_header()
        self._apply_mode()

    def _refresh_header(self) -> None:
        d = self._doc
        if d is None:
            return
        # source badge
        if d.source is ContextSource.USER:
            self.source_badge.setText(" USER ")
            self.source_badge.setStyleSheet(
                f"background-color: {COLOR_PRIMARY_CONTAINER}; "
                f"color: {COLOR_ON_SURFACE}; "
                f"border-radius: {RADIUS_FULL}px; "
                f"padding: 0 {SPACING_SM}px; "
                f"font-size: 11px; font-weight: 600; letter-spacing: 0.05em;"
            )
        else:
            self.source_badge.setText(" GENERATED ")
            self.source_badge.setStyleSheet(
                f"background-color: {COLOR_WARNING_BG}; "
                f"color: {COLOR_WARNING}; "
                f"border-radius: {RADIUS_FULL}px; "
                f"padding: 0 {SPACING_SM}px; "
                f"font-size: 11px; font-weight: 600; letter-spacing: 0.05em;"
            )
        # meta
        if d.exists():
            ts = d.updated or d.created or ""
            if ts:
                self.meta_label.setText(f"updated {ts}")
            else:
                self.meta_label.setText("")
        else:
            self.meta_label.setText("not yet generated")

    def _apply_mode(self) -> None:
        d = self._doc
        if d is None:
            return
        # User slots are always editable. Programmatic slots are read-only
        # until they exist on disk (i.e. have been generated); the page
        # also disables their tab entirely until then.
        editable = d.source is ContextSource.USER or d.exists()
        self.editor.setReadOnly(not editable)

    # --- Events -------------------------------------------------------------
    def _on_text_changed(self) -> None:
        self._dirty = True
        # restart the debounce timer — save AUTOSAVE_DEBOUNCE_MS after the
        # last keystroke.
        self._timer.start(AUTOSAVE_DEBOUNCE_MS)

    def _cancel_pending_save(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def _flush_save(self) -> None:
        """Timer callback: persist current editor content to disk."""
        if self._doc is None or not self._dirty:
            return
        content = self.editor.toPlainText()
        self._doc = self._store.save(self._video_stem, self._ctype, content)
        self._dirty = False
        self._refresh_header()
        self.save_requested.emit(content)

    def save_now(self) -> None:
        """Persist immediately, cancelling any pending debounced save.

        Called by the page on navigation (Back/Continue) and on focus loss.
        """
        self._cancel_pending_save()
        if self._doc is None or not self._dirty:
            return
        self._flush_save()

    def focusOutEvent(self, event):
        # immediate save on focus loss
        if self._dirty:
            self.save_now()
        super().focusOutEvent(event)