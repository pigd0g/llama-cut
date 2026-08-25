from pathlib import Path

import pytest

from src.context import ContextStore, ContextType
from src.context_review import (
    FRAME_ANALYSIS_HEADING,
    PROJECT_HEADING,
    TRANSCRIPTION_HEADING,
    VIDEO_HEADING,
    AssembledDocument,
    VideoSection,
    assemble_markdown,
    build_export_markdown,
    find_frame_filenames_in_frame_analysis,
    load_assembled,
    markdown_to_html,
    parse_markdown_back,
    split_sections_for_edit,
)


# --- Helpers ----------------------------------------------------------------

def _make_state(videos):
    """Build a minimal state-like object with selected_videos."""
    return _State(videos)


class _V:
    def __init__(self, stem, name, thumb=""):
        self.stem = stem
        self.name = name
        self.thumbnail_path = thumb


class _State:
    def __init__(self, vids):
        self.selected_videos = vids


def _make_doc(project="Project about Birds.", videos=None):
    return AssembledDocument(
        project_context=project,
        videos=videos or [],
    )


def _make_video(stem="ClipA", name="ClipA.mp4", thumb="/tmp/ClipA.jpg",
                vc="A nature walk.", tc="# Transcription\n\nHello world.",
                fa="# Frame Analysis\n\n## Frame ClipA-00-00-01-000-0001.jpg — 00:00:01.000 (#1)\n\nScene:\nA forest."):
    return VideoSection(
        stem=stem, name=name, thumbnail_path=thumb,
        video_context=vc, transcription=tc, frame_analysis=fa,
    )


# --- load_assembled ---------------------------------------------------------

def test_load_assembled_reads_all_sections(tmp_path):
    store = ContextStore(tmp_path / "context")
    store.save(None, ContextType.PROJECT, "# Project Context\n\nBirds.")
    store.save("ClipA", ContextType.VIDEO, "# Video Context\n\nA walk.")
    store.save("ClipA", ContextType.TRANSCRIPTION, "# Transcription\n\nHello.")
    store.save("ClipA", ContextType.FRAME_ANALYSIS, "# Frame Analysis\n\nFrame 1.")
    state = _make_state([_V("ClipA", "ClipA.mp4", "/tmp/ClipA.jpg")])
    doc = load_assembled(state, store)
    assert "Birds." in doc.project_context
    assert len(doc.videos) == 1
    v = doc.videos[0]
    assert v.stem == "ClipA"
    assert v.name == "ClipA.mp4"
    assert v.thumbnail_path == "/tmp/ClipA.jpg"
    assert "A walk." in v.video_context
    assert "Hello." in v.transcription
    assert "Frame 1." in v.frame_analysis


def test_load_assembled_missing_sections_become_empty(tmp_path):
    store = ContextStore(tmp_path / "context")
    state = _make_state([_V("ClipA", "ClipA.mp4", "")])
    doc = load_assembled(state, store)
    assert doc.project_context == ""
    v = doc.videos[0]
    assert v.video_context == ""
    assert v.transcription == ""
    assert v.frame_analysis == ""


def test_load_assembled_no_videos(tmp_path):
    store = ContextStore(tmp_path / "context")
    state = _make_state([])
    doc = load_assembled(state, store)
    assert doc.videos == []
    assert doc.project_context == ""


# --- assemble_markdown ------------------------------------------------------

def test_assemble_markdown_structure_project_at_top():
    doc = _make_doc(project="My project.", videos=[_make_video()])
    md = assemble_markdown(doc)
    # Project heading must be the first line.
    assert md.startswith("# Project Context\n")
    assert "My project." in md
    # Video block heading follows.
    assert "# ClipA.mp4" in md
    # H2 sections in canonical order.
    assert md.index("## Video Context") < md.index("## Transcription")
    assert md.index("## Transcription") < md.index("## Frame Analysis")


def test_assemble_markdown_includes_thumbnail_image(tmp_path):
    # Create the thumbnail file so the image link is included.
    thumb = tmp_path / "ClipA.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xe0fake")
    v = _make_video(thumb=str(thumb))
    doc = _make_doc(videos=[v])
    md = assemble_markdown(doc)
    assert f"![thumbnail]({thumb.as_posix()})" in md


def test_assemble_markdown_omits_thumbnail_if_missing(tmp_path):
    v = _make_video(thumb=str(tmp_path / "nonexistent.jpg"))
    doc = _make_doc(videos=[v])
    md = assemble_markdown(doc)
    assert "![thumbnail]" not in md


def test_assemble_markdown_empty_project_uses_placeholder():
    doc = _make_doc(project="", videos=[])
    md = assemble_markdown(doc)
    assert "_No project context provided._" in md


def test_assemble_markdown_strips_duplicate_leading_heading():
    # If the stored content already starts with "# Project Context", the
    # assembler should not duplicate it.
    doc = _make_doc(project="# Project Context\n\nReal content.", videos=[])
    md = assemble_markdown(doc)
    # Exactly one "# Project Context" heading.
    assert md.count("# Project Context") == 1
    assert "Real content." in md


def test_assemble_markdown_multiple_videos():
    v1 = _make_video(stem="ClipA", name="ClipA.mp4")
    v2 = _make_video(stem="ClipB", name="ClipB.mp4", vc="Clip B context.")
    doc = _make_doc(videos=[v1, v2])
    md = assemble_markdown(doc)
    assert "# ClipA.mp4" in md
    assert "# ClipB.mp4" in md
    assert md.index("# ClipA.mp4") < md.index("# ClipB.mp4")
    assert "Clip B context." in md


# --- parse_markdown_back ----------------------------------------------------

def test_parse_markdown_back_roundtrip():
    v = _make_video()
    doc = _make_doc(videos=[v])
    md = assemble_markdown(doc)
    parsed = parse_markdown_back(md, [("ClipA", "ClipA.mp4")])
    assert "Birds." in parsed["project"]
    v_out = parsed["videos"]["ClipA"]
    assert "A nature walk." in v_out["video"]
    assert "Hello world." in v_out["transcription"]
    assert "A forest." in v_out["frame_analysis"]


def test_parse_markdown_back_empty_document():
    parsed = parse_markdown_back("", [("ClipA", "ClipA.mp4")])
    assert parsed["project"] == ""
    assert parsed["videos"]["ClipA"]["video"] == ""
    assert parsed["videos"]["ClipA"]["transcription"] == ""
    assert parsed["videos"]["ClipA"]["frame_analysis"] == ""


def test_parse_markdown_back_missing_h2_sections():
    # A video block with only one H2 section — the others should be empty.
    md = """# Project Context

My project.

# ClipA.mp4

## Video Context

Only this section exists.
"""
    parsed = parse_markdown_back(md, [("ClipA", "ClipA.mp4")])
    assert "My project." in parsed["project"]
    v = parsed["videos"]["ClipA"]
    assert "Only this section exists." in v["video"]
    assert v["transcription"] == ""
    assert v["frame_analysis"] == ""


def test_parse_markdown_back_missing_video_block():
    # Document with project but no video blocks.
    md = "# Project Context\n\nJust project."
    parsed = parse_markdown_back(md, [("ClipA", "ClipA.mp4")])
    assert "Just project." in parsed["project"]
    assert parsed["videos"]["ClipA"]["video"] == ""


def test_parse_markdown_back_malformed_heading_no_crash():
    md = "Some text without headings."
    parsed = parse_markdown_back(md, [("ClipA", "ClipA.mp4")])
    # Should not raise; sections stay empty.
    assert parsed["project"] == ""
    assert parsed["videos"]["ClipA"]["video"] == ""


def test_parse_markdown_back_preserves_content_verbatim():
    vc = "Line 1\nLine 2\n\n- bullet\n- bullet 2"
    v = _make_video(vc=vc)
    doc = _make_doc(videos=[v])
    md = assemble_markdown(doc)
    parsed = parse_markdown_back(md, [("ClipA", "ClipA.mp4")])
    assert parsed["videos"]["ClipA"]["video"] == vc


# --- find_frame_filenames_in_frame_analysis ---------------------------------

def test_find_frame_filenames_single():
    fa = "## Frame ClipA-00-00-01-000-0001.jpg — 00:00:01.000 (#1)\n\nScene:\nA forest."
    names = find_frame_filenames_in_frame_analysis(fa)
    assert names == ["ClipA-00-00-01-000-0001.jpg"]


def test_find_frame_filenames_multiple_in_order():
    fa = (
        "## Frame ClipA-00-00-01-000-0001.jpg — 00:00:01.000 (#1)\n\nScene 1.\n\n"
        "## Frame ClipA-00-00-05-000-0002.jpg — 00:00:05.000 (#2)\n\nScene 2.\n\n"
    )
    names = find_frame_filenames_in_frame_analysis(fa)
    assert names == [
        "ClipA-00-00-01-000-0001.jpg",
        "ClipA-00-00-05-000-0002.jpg",
    ]


def test_find_frame_filenames_empty():
    assert find_frame_filenames_in_frame_analysis("") == []
    assert find_frame_filenames_in_frame_analysis("No frame headings here.") == []


def test_find_frame_filenames_ignores_non_frame_h2():
    fa = "## Video Context\n\nSome text.\n\n## Frame real.jpg — 00:00:01.000 (#1)\n\nBody."
    names = find_frame_filenames_in_frame_analysis(fa)
    assert names == ["real.jpg"]


# --- markdown_to_html -------------------------------------------------------

def test_markdown_to_html_h1_h2_h3():
    md = "# Title\n\n## Subtitle\n\n### Sub-subtitle"
    html = markdown_to_html(md)
    assert "<h1>Title</h1>" in html
    assert "<h2>Subtitle</h2>" in html
    assert "<h3>Sub-subtitle</h3>" in html


def test_markdown_to_html_paragraphs():
    md = "First paragraph.\n\nSecond paragraph."
    html = markdown_to_html(md)
    assert "<p>First paragraph.</p>" in html
    assert "<p>Second paragraph.</p>" in html


def test_markdown_to_html_unordered_list():
    md = "- Item 1\n- Item 2\n- Item 3"
    html = markdown_to_html(md)
    assert "<ul>" in html
    assert "</ul>" in html
    assert "<li>Item 1</li>" in html
    assert "<li>Item 2</li>" in html
    assert "<li>Item 3</li>" in html


def test_markdown_to_html_ordered_list():
    md = "1. First\n2. Second"
    html = markdown_to_html(md)
    assert "<ol>" in html
    assert "</ol>" in html
    assert "<li>First</li>" in html
    assert "<li>Second</li>" in html


def test_markdown_to_html_bold_and_italic():
    md = "This is **bold** and *italic*."
    html = markdown_to_html(md)
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html


def test_markdown_to_html_inline_code():
    md = "Use `pip install` to install."
    html = markdown_to_html(md)
    assert "<code>pip install</code>" in html


def test_markdown_to_html_escapes_html_special_chars():
    md = "Use <script> and & entities."
    html = markdown_to_html(md)
    assert "&lt;script&gt;" in html
    assert "&amp; entities" in html


def test_markdown_to_html_inserts_frame_image(tmp_path):
    # Create a frame image file.
    frame_path = tmp_path / "ClipA-00-00-01-000-0001.jpg"
    frame_path.write_bytes(b"\xff\xd8\xff\xe0fake")
    md = f"## Frame {frame_path.name} — 00:00:01.000 (#1)\n\nScene:\nA forest."
    frame_map = {frame_path.name: str(frame_path)}
    html = markdown_to_html(md, frame_map)
    assert "<img" in html
    assert frame_path.as_uri() in html


def test_markdown_to_html_no_image_when_frame_missing(tmp_path):
    md = "## Frame missing.jpg — 00:00:01.000 (#1)\n\nScene."
    frame_map = {"missing.jpg": str(tmp_path / "missing.jpg")}
    html = markdown_to_html(md, frame_map)
    assert "<img" not in html


def test_markdown_to_html_thumbnail_image(tmp_path):
    thumb = tmp_path / "ClipA.jpg"
    thumb.write_bytes(b"fake")
    md = f"![thumbnail]({thumb.as_posix()})"
    html = markdown_to_html(md)
    assert "<img" in html
    assert thumb.as_uri() in html


def test_markdown_to_html_empty():
    assert markdown_to_html("") == ""


# --- GFM tables --------------------------------------------------------------

def test_markdown_to_html_table_basic():
    md = (
        "| Shot | Source | Duration |\n"
        "| --- | --- | --- |\n"
        "| Hook | GX012053.MP4 | 10s |\n"
        "| B-roll | GX012054.MP4 | 8s |"
    )
    html = markdown_to_html(md)
    assert "<table" in html
    assert "<thead>" in html
    assert "<tbody>" in html
    assert "<th>Shot</th>" in html
    assert "<th>Source</th>" in html
    assert "<td>Hook</td>" in html
    assert "<td>GX012053.MP4</td>" in html
    assert "<td>B-roll</td>" in html
    # No stray paragraph wrapping the table rows.
    assert "<p>| Shot" not in html


def test_markdown_to_html_table_inline_formatting():
    md = (
        "| Scene | Notes |\n"
        "| --- | --- |\n"
        "| 1 | **Opening** hook |\n"
        "| 2 | `cut` on beat |"
    )
    html = markdown_to_html(md)
    assert "<strong>Opening</strong>" in html
    assert "<code>cut</code>" in html


def test_markdown_to_html_table_alignment():
    md = (
        "| Left | Center | Right |\n"
        "| :--- | :---: | ---: |\n"
        "| a | b | c |"
    )
    html = markdown_to_html(md)
    assert 'text-align:left' in html
    assert 'text-align:center' in html
    assert 'text-align:right' in html


def test_markdown_to_html_table_escaped_pipe():
    md = (
        "| Name | Value |\n"
        "| --- | --- |\n"
        "| a\\|b | 1 |"
    )
    html = markdown_to_html(md)
    assert "<td>a|b</td>" in html


def test_markdown_to_html_table_surrounded_by_paragraphs():
    md = (
        "Intro text.\n\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n\n"
        "Outro text."
    )
    html = markdown_to_html(md)
    assert "<p>Intro text.</p>" in html
    assert "<table" in html
    assert "<p>Outro text.</p>" in html


def test_markdown_to_html_pipe_line_without_separator_is_paragraph():
    """A pipe line that isn't followed by a separator row stays a paragraph."""
    md = "Just a | pipe line"
    html = markdown_to_html(md)
    assert "<table" not in html
    assert "<p>Just a | pipe line</p>" in html


# --- build_export_markdown --------------------------------------------------

def test_build_export_markdown_structure(tmp_path):
    thumb = tmp_path / "ClipA.jpg"
    thumb.write_bytes(b"fake")
    v = _make_video(thumb=str(thumb))
    doc = _make_doc(videos=[v])
    md = build_export_markdown(doc, {})
    assert md.startswith("# Video Context Report")
    assert "# Project Context" in md
    assert "# ClipA.mp4" in md
    assert "## Video Context" in md
    assert "## Transcription" in md
    assert "## Frame Analysis" in md
    assert f"![thumbnail]({thumb.as_posix()})" in md


def test_build_export_markdown_injects_frame_images(tmp_path):
    frame_path = tmp_path / "ClipA-00-00-01-000-0001.jpg"
    frame_path.write_bytes(b"fake")
    v = _make_video()
    doc = _make_doc(videos=[v])
    frame_map = {frame_path.name: str(frame_path)}
    md = build_export_markdown(doc, frame_map)
    # The frame image link should appear after the frame heading.
    assert f"![{frame_path.name}]({frame_path.as_posix()})" in md
    # Verify it appears after the frame heading, not before.
    heading_pos = md.index(f"## Frame {frame_path.name}")
    img_pos = md.index(f"![{frame_path.name}]")
    assert img_pos > heading_pos


def test_build_export_markdown_no_frame_image_when_missing(tmp_path):
    v = _make_video()
    doc = _make_doc(videos=[v])
    frame_map = {"ClipA-00-00-01-000-0001.jpg": str(tmp_path / "missing.jpg")}
    md = build_export_markdown(doc, frame_map)
    # Frame heading should still be present, but no image link.
    assert "## Frame ClipA-00-00-01-000-0001.jpg" in md
    assert "![" not in md.split("## Frame ClipA-00-00-01-000-0001.jpg")[1].split("\n")[0]


def test_build_export_markdown_empty_sections_use_placeholders():
    doc = _make_doc(project="", videos=[])
    md = build_export_markdown(doc, {})
    assert "_No project context provided._" in md


# --- split_sections_for_edit ------------------------------------------------

def test_split_sections_for_edit_keys():
    v = _make_video()
    doc = _make_doc(videos=[v])
    md = assemble_markdown(doc)
    sections = split_sections_for_edit(md, [("ClipA", "ClipA.mp4")])
    assert "project" in sections
    assert "ClipA::video" in sections
    assert "ClipA::transcription" in sections
    assert "ClipA::frame_analysis" in sections


def test_split_sections_for_edit_roundtrip():
    v = _make_video()
    doc = _make_doc(videos=[v])
    md = assemble_markdown(doc)
    sections = split_sections_for_edit(md, [("ClipA", "ClipA.mp4")])
    assert "Birds." in sections["project"]
    assert "A nature walk." in sections["ClipA::video"]
    assert "Hello world." in sections["ClipA::transcription"]
    assert "A forest." in sections["ClipA::frame_analysis"]


# --- Integration: assemble → parse roundtrip with multiple videos -----------

def test_assemble_parse_roundtrip_multiple_videos():
    v1 = _make_video(stem="ClipA", name="ClipA.mp4",
                     vc="Clip A video context.",
                     tc="# Transcription\n\nClip A transcript.",
                     fa="# Frame Analysis\n\n## Frame A-00-00-01-000-0001.jpg — 00:00:01.000 (#1)\n\nScene A.")
    v2 = _make_video(stem="ClipB", name="ClipB.mp4",
                     vc="Clip B video context.",
                     tc="# Transcription\n\nClip B transcript.",
                     fa="# Frame Analysis\n\n## Frame B-00-00-02-000-0001.jpg — 00:00:02.000 (#1)\n\nScene B.")
    doc = _make_doc(project="Multi-video project.", videos=[v1, v2])
    md = assemble_markdown(doc)
    parsed = parse_markdown_back(md, [("ClipA", "ClipA.mp4"), ("ClipB", "ClipB.mp4")])
    assert "Multi-video project." in parsed["project"]
    assert "Clip A video context." in parsed["videos"]["ClipA"]["video"]
    assert "Clip A transcript." in parsed["videos"]["ClipA"]["transcription"]
    assert "Scene A." in parsed["videos"]["ClipA"]["frame_analysis"]
    assert "Clip B video context." in parsed["videos"]["ClipB"]["video"]
    assert "Clip B transcript." in parsed["videos"]["ClipB"]["transcription"]
    assert "Scene B." in parsed["videos"]["ClipB"]["frame_analysis"]