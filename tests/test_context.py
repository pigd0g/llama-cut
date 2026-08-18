import json
from pathlib import Path

import pytest

from src.context import (
    DEFAULT_SOURCE,
    PROJECT_FILENAME,
    ContextDoc,
    ContextSource,
    ContextStore,
    ContextType,
)


# --- Default source mapping -------------------------------------------------

def test_default_source_project_is_user():
    assert DEFAULT_SOURCE[ContextType.PROJECT] is ContextSource.USER


def test_default_source_video_is_user():
    assert DEFAULT_SOURCE[ContextType.VIDEO] is ContextSource.USER


def test_default_source_frame_analysis_is_programmatic():
    assert DEFAULT_SOURCE[ContextType.FRAME_ANALYSIS] is ContextSource.PROGRAMMATIC


def test_default_source_transcription_is_programmatic():
    assert DEFAULT_SOURCE[ContextType.TRANSCRIPTION] is ContextSource.PROGRAMMATIC


# --- Store: save + get -----------------------------------------------------

def test_save_creates_file_and_manifest_entry(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save(None, ContextType.PROJECT, "# Project\n\nHello")
    md = tmp_path / "context" / PROJECT_FILENAME
    assert md.exists()
    assert md.read_text(encoding="utf-8") == "# Project\n\nHello"
    manifest = json.loads((tmp_path / "context" / "manifest.json").read_text("utf-8"))
    assert manifest["project"]["type"] == "project"
    assert manifest["project"]["source"] == "user"
    assert manifest["project"]["file"] == PROJECT_FILENAME
    assert manifest["project"]["created"]
    assert manifest["project"]["updated"]


def test_save_video_creates_stemmed_filename(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save("ClipA", ContextType.VIDEO, "# Video context")
    assert (tmp_path / "context" / "ClipA_video.md").exists()
    manifest = json.loads((tmp_path / "context" / "manifest.json").read_text("utf-8"))
    assert manifest["videos"]["ClipA"]["video"]["file"] == "ClipA_video.md"


def test_save_frame_analysis_uses_programmatic_source(tmp_path):
    store = ContextStore(tmp_path / "context")
    doc = store.save("ClipA", ContextType.FRAME_ANALYSIS, "# Frame analysis")
    assert doc.source is ContextSource.PROGRAMMATIC
    assert (tmp_path / "context" / "ClipA_frame_analysis.md").exists()


def test_save_transcription_uses_programmatic_source(tmp_path):
    store = ContextStore(tmp_path / "context")
    doc = store.save("ClipA", ContextType.TRANSCRIPTION, "# Transcription")
    assert doc.source is ContextSource.PROGRAMMATIC
    assert (tmp_path / "context" / "ClipA_transcription.md").exists()


# --- Round-trip ------------------------------------------------------------

def test_get_returns_none_for_missing_slot(tmp_path):
    store = ContextStore(tmp_path / "context")
    assert store.get(None, ContextType.PROJECT) is None
    assert store.get("ClipA", ContextType.VIDEO) is None


def test_get_loads_content_from_file(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save("ClipA", ContextType.VIDEO, "# My video\n\ncontents")
    doc = store.get("ClipA", ContextType.VIDEO)
    assert doc is not None
    assert doc.type is ContextType.VIDEO
    assert doc.source is ContextSource.USER
    assert doc.content == "# My video\n\ncontents"
    assert doc.exists()


# --- Source immutability ---------------------------------------------------

def test_source_never_changes_on_edit(tmp_path):
    """Saving a programmatic slot keeps source == programmatic."""
    store = ContextStore(tmp_path / "context")
    store.save("ClipA", ContextType.FRAME_ANALYSIS, "# generated v1")
    store.save("ClipA", ContextType.FRAME_ANALYSIS, "# user edited v2")
    doc = store.get("ClipA", ContextType.FRAME_ANALYSIS)
    assert doc is not None
    assert doc.source is ContextSource.PROGRAMMATIC
    assert doc.content == "# user edited v2"


def test_updated_bumps_but_created_stays(tmp_path):
    store = ContextStore(tmp_path / "context")
    d1 = store.save(None, ContextType.PROJECT, "v1")
    created_first = d1.created
    d2 = store.save(None, ContextType.PROJECT, "v2")
    assert d2.created == created_first
    assert d2.updated >= d1.updated


# --- Project vs video ------------------------------------------------------

def test_video_stem_none_is_project(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save(None, ContextType.PROJECT, "proj")
    doc = store.get(None, ContextType.PROJECT)
    assert doc is not None
    assert doc.file_path.name == PROJECT_FILENAME


def test_project_and_video_have_different_files(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save(None, ContextType.PROJECT, "proj")
    store.save("ClipA", ContextType.VIDEO, "vid")
    assert (tmp_path / "context" / PROJECT_FILENAME).exists()
    assert (tmp_path / "context" / "ClipA_video.md").exists()


# --- list_slots ------------------------------------------------------------

def test_list_slots_for_project_returns_one(tmp_path):
    store = ContextStore(tmp_path / "context")
    slots = store.list_slots(None)
    assert len(slots) == 1
    assert slots[0].type is ContextType.PROJECT


def test_list_slots_for_video_returns_three(tmp_path):
    store = ContextStore(tmp_path / "context")
    slots = store.list_slots("ClipA")
    assert len(slots) == 3
    types = {s.type for s in slots}
    assert types == {ContextType.VIDEO, ContextType.FRAME_ANALYSIS,
                     ContextType.TRANSCRIPTION}


def test_list_slots_returns_empty_doc_for_unsaved(tmp_path):
    store = ContextStore(tmp_path / "context")
    slots = store.list_slots("ClipA")
    for s in slots:
        assert s.content == ""
        assert not s.exists()


def test_list_slots_returns_saved_content(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save("ClipA", ContextType.VIDEO, "# saved")
    slots = store.list_slots("ClipA")
    video_slot = next(s for s in slots if s.type is ContextType.VIDEO)
    assert video_slot.content == "# saved"
    assert video_slot.exists()


# --- Manifest / file drift -------------------------------------------------

def test_manifest_missing_file_returns_doc_with_empty_content(tmp_path):
    """If the manifest entry exists but the .md was deleted externally."""
    store = ContextStore(tmp_path / "context")
    store.save("ClipA", ContextType.VIDEO, "x")
    # delete the md file but keep the manifest
    (tmp_path / "context" / "ClipA_video.md").unlink()
    doc = store.get("ClipA", ContextType.VIDEO)
    assert doc is not None
    assert doc.content == ""
    assert not doc.exists()


def test_load_manifest_returns_default_when_missing(tmp_path):
    store = ContextStore(tmp_path / "context")
    manifest = store.load_manifest()
    assert manifest["version"] == 1
    assert manifest["project"] is None
    assert manifest["videos"] == {}


# --- Filenames -------------------------------------------------------------

def test_filename_project():
    from src.context import _file_name
    assert _file_name(None, ContextType.PROJECT) == PROJECT_FILENAME


def test_filename_video():
    from src.context import _file_name
    assert _file_name("ClipA", ContextType.VIDEO) == "ClipA_video.md"


def test_filename_frame_analysis():
    from src.context import _file_name
    assert _file_name("ClipA", ContextType.FRAME_ANALYSIS) == "ClipA_frame_analysis.md"


def test_filename_transcription():
    from src.context import _file_name
    assert _file_name("ClipA", ContextType.TRANSCRIPTION) == "ClipA_transcription.md"


def test_filename_sanitizes_stem(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save("My Clip/A", ContextType.VIDEO, "x")
    # sanitized stem should not contain /
    files = list((tmp_path / "context").glob("*.md"))
    assert all("/" not in f.name for f in files)


# --- ContextDoc.exists -----------------------------------------------------

def test_contextdoc_exists_true_after_save(tmp_path):
    store = ContextStore(tmp_path / "context")
    doc = store.save(None, ContextType.PROJECT, "x")
    assert doc.exists()


def test_contextdoc_to_manifest_entry_roundtrip(tmp_path):
    store = ContextStore(tmp_path / "context")
    doc = store.save(None, ContextType.PROJECT, "x")
    entry = doc.to_manifest_entry()
    assert entry["type"] == "project"
    assert entry["source"] == "user"
    assert entry["file"] == PROJECT_FILENAME