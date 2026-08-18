from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


# --- Enums ------------------------------------------------------------------

class ContextType(str, Enum):
    PROJECT = "project"
    VIDEO = "video"
    FRAME_ANALYSIS = "frame_analysis"
    TRANSCRIPTION = "transcription"


class ContextSource(str, Enum):
    USER = "user"
    PROGRAMMATIC = "programmatic"


# Fixed source per slot type — never changes on edit.
DEFAULT_SOURCE: dict[ContextType, ContextSource] = {
    ContextType.PROJECT: ContextSource.USER,
    ContextType.VIDEO: ContextSource.USER,
    ContextType.FRAME_ANALYSIS: ContextSource.PROGRAMMATIC,
    ContextType.TRANSCRIPTION: ContextSource.PROGRAMMATIC,
}

# Video-level slots (PROJECT is handled separately)
VIDEO_SLOTS: tuple[ContextType, ...] = (
    ContextType.VIDEO,
    ContextType.FRAME_ANALYSIS,
    ContextType.TRANSCRIPTION,
)

MANIFEST_VERSION = 1
PROJECT_FILENAME = "project.md"


# --- Dataclass --------------------------------------------------------------

@dataclass
class ContextDoc:
    type: ContextType
    source: ContextSource
    file_path: Path
    created: str
    updated: str
    content: str

    def exists(self) -> bool:
        return self.file_path.exists() and self.file_path.is_file()

    def to_manifest_entry(self) -> dict:
        return {
            "type": self.type.value,
            "source": self.source.value,
            "file": self.file_path.name,
            "created": self.created,
            "updated": self.updated,
        }


# --- Store ------------------------------------------------------------------

class ContextStore:
    """Reads/writes Markdown context files + a sidecar manifest.json.

    `video_stem=None` means Project Context. Video-level slots use the stem
    to build filenames: `<stem>_<type>.md`.
    """

    def __init__(self, context_dir: Path):
        self._dir = Path(context_dir)

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def manifest_path(self) -> Path:
        return self._dir / "manifest.json"

    # --- File paths --------------------------------------------------------
    def _file_for(self, video_stem: Optional[str], ctype: ContextType) -> Path:
        if ctype is ContextType.PROJECT:
            return self._dir / PROJECT_FILENAME
        stem = _sanitize(video_stem or "")
        return self._dir / f"{stem}_{ctype.value}.md"

    # --- Manifest ----------------------------------------------------------
    def load_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"version": MANIFEST_VERSION, "project": None, "videos": {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": MANIFEST_VERSION, "project": None, "videos": {}}
        data.setdefault("version", MANIFEST_VERSION)
        data.setdefault("project", None)
        data.setdefault("videos", {})
        return data

    def save_manifest(self, manifest: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        manifest["version"] = MANIFEST_VERSION
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )

    # --- Read --------------------------------------------------------------
    def get(self, video_stem: Optional[str], ctype: ContextType) -> Optional[ContextDoc]:
        manifest = self.load_manifest()
        entry = _manifest_get(manifest, video_stem, ctype)
        if not entry:
            return None
        try:
            ctype_val = ContextType(entry.get("type", ctype.value))
            source_val = ContextSource(entry.get("source", DEFAULT_SOURCE[ctype].value))
        except ValueError:
            ctype_val = ctype
            source_val = DEFAULT_SOURCE[ctype]
        file_path = self._dir / entry.get("file", _file_name(video_stem, ctype))
        content = _read_text(file_path)
        return ContextDoc(
            type=ctype_val,
            source=source_val,
            file_path=file_path,
            created=entry.get("created", ""),
            updated=entry.get("updated", ""),
            content=content,
        )

    # --- Write -------------------------------------------------------------
    def save(self, video_stem: Optional[str], ctype: ContextType,
             content: str) -> ContextDoc:
        self._dir.mkdir(parents=True, exist_ok=True)
        file_path = self._file_for(video_stem, ctype)
        file_path.write_text(content, encoding="utf-8")

        manifest = self.load_manifest()
        now = _now_iso()
        entry = _manifest_get(manifest, video_stem, ctype)
        if entry:
            created = entry.get("created", now)
            source_val = ContextSource(entry.get("source", DEFAULT_SOURCE[ctype].value))
        else:
            created = now
            source_val = DEFAULT_SOURCE[ctype]
        new_entry = {
            "type": ctype.value,
            "source": source_val.value,
            "file": file_path.name,
            "created": created,
            "updated": now,
        }
        _manifest_set(manifest, video_stem, ctype, new_entry)
        self.save_manifest(manifest)
        return ContextDoc(
            type=ctype,
            source=source_val,
            file_path=file_path,
            created=created,
            updated=now,
            content=content,
        )

    # --- Listing -----------------------------------------------------------
    def list_slots(self, video_stem: Optional[str]) -> list[ContextDoc]:
        """Return one ContextDoc per slot for the given scope (project or video).

        Slots with no manifest entry are returned with empty content and
        default source, so the UI can render all slots uniformly.
        """
        if video_stem is None:
            return [self._slot_or_empty(None, ContextType.PROJECT)]
        return [self._slot_or_empty(video_stem, ct) for ct in VIDEO_SLOTS]

    def _slot_or_empty(self, video_stem: Optional[str], ctype: ContextType) -> ContextDoc:
        existing = self.get(video_stem, ctype)
        if existing:
            return existing
        file_path = self._file_for(video_stem, ctype)
        return ContextDoc(
            type=ctype,
            source=DEFAULT_SOURCE[ctype],
            file_path=file_path,
            created="",
            updated="",
            content="",
        )

    def list_video_stems(self) -> list[str]:
        manifest = self.load_manifest()
        return sorted(manifest.get("videos", {}).keys())


# --- Helpers ----------------------------------------------------------------

def _sanitize(stem: str) -> str:
    out = []
    for ch in stem:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    return s[:120] if len(s) > 120 else s


def _file_name(video_stem: Optional[str], ctype: ContextType) -> str:
    if ctype is ContextType.PROJECT:
        return PROJECT_FILENAME
    return f"{_sanitize(video_stem or '')}_{ctype.value}.md"


def _manifest_get(manifest: dict, video_stem: Optional[str],
                   ctype: ContextType) -> Optional[dict]:
    if ctype is ContextType.PROJECT:
        return manifest.get("project") or None
    videos = manifest.get("videos", {})
    entry = videos.get(video_stem or "")
    if not entry:
        return None
    return entry.get(ctype.value) or None


def _manifest_set(manifest: dict, video_stem: Optional[str],
                   ctype: ContextType, value: dict) -> None:
    if ctype is ContextType.PROJECT:
        manifest["project"] = value
        return
    videos = manifest.setdefault("videos", {})
    slot = videos.setdefault(video_stem or "", {})
    slot[ctype.value] = value


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# --- Re-exports for convenience --------------------------------------------

# Expose the state.py sanitizer so callers can reuse the same normalization
# without duplicating logic. Imported lazily to avoid a circular import.
def video_stem_for(path_str: str) -> str:
    """Normalize a video path/filename to the same stem used for context files."""
    from .state import _sanitize_stem
    from pathlib import PurePath
    return _sanitize_stem(PurePath(path_str).stem)