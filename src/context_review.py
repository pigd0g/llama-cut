from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --- Section headings -------------------------------------------------------

# Canonical H1 headings used to delimit sections in the assembled document.
# These double as the markdown H1 the user sees and the parser's split points.
PROJECT_HEADING = "# Project Context"
VIDEO_HEADING = "# Video Context"
TRANSCRIPTION_HEADING = "# Transcription"
FRAME_ANALYSIS_HEADING = "# Frame Analysis"

# Per-video H2 section headings inside a video block.
VIDEO_SECTION_H2 = "## Video Context"
TRANSCRIPTION_SECTION_H2 = "## Transcription"
FRAME_ANALYSIS_SECTION_H2 = "## Frame Analysis"

# The H1 used for each video block (followed by the thumbnail image).
def video_block_heading(name: str) -> str:
    return f"# {name}"


# --- Dataclasses ------------------------------------------------------------

@dataclass
class VideoSection:
    stem: str
    name: str
    thumbnail_path: str
    video_context: str = ""
    transcription: str = ""
    frame_analysis: str = ""


@dataclass
class AssembledDocument:
    project_context: str = ""
    videos: list[VideoSection] = field(default_factory=list)


# --- Loading from the ContextStore ------------------------------------------

def load_assembled(state, store) -> AssembledDocument:
    """Read project + per-video sections from the ContextStore.

    Uses state.selected_videos for ordering. Missing sections become empty
    strings (never None) so the rest of the pipeline can treat them uniformly.
    """
    from .context import ContextType

    project_doc = store.get(None, ContextType.PROJECT)
    project_ctx = project_doc.content if project_doc and project_doc.content else ""

    videos: list[VideoSection] = []
    for v in state.selected_videos:
        vdoc = store.get(v.stem, ContextType.VIDEO)
        tdoc = store.get(v.stem, ContextType.TRANSCRIPTION)
        fdoc = store.get(v.stem, ContextType.FRAME_ANALYSIS)
        videos.append(VideoSection(
            stem=v.stem,
            name=v.name,
            thumbnail_path=v.thumbnail_path or "",
            video_context=vdoc.content if vdoc and vdoc.content else "",
            transcription=tdoc.content if tdoc and tdoc.content else "",
            frame_analysis=fdoc.content if fdoc and fdoc.content else "",
        ))
    return AssembledDocument(project_context=project_ctx, videos=videos)


# --- Assembling the single markdown document --------------------------------

def assemble_markdown(doc: AssembledDocument) -> str:
    """Build the single markdown document: project at top, then per-video.

    Structure:
      # Project Context
      <project content>

      # <video 1 name>
      ![thumbnail](<abs_path>)

      ## Video Context
      <video content>

      ## Transcription
      <transcription content>

      ## Frame Analysis
      <frame analysis content>

      # <video 2 name>
      ...
    """
    parts: list[str] = []

    # Project context (strip a leading "# Project Context" if already present
    # in the stored content so we don't duplicate the heading).
    project_body = _strip_leading_heading(doc.project_context, PROJECT_HEADING)
    parts.append(PROJECT_HEADING)
    parts.append("")
    parts.append(project_body.strip() if project_body.strip()
                 else "_No project context provided._")
    parts.append("")

    for v in doc.videos:
        parts.append(video_block_heading(v.name))
        parts.append("")
        if v.thumbnail_path and Path(v.thumbnail_path).exists():
            parts.append(f"![thumbnail]({Path(v.thumbnail_path).as_posix()})")
            parts.append("")
        parts.append(VIDEO_SECTION_H2)
        parts.append("")
        parts.append(_strip_leading_heading(v.video_context, VIDEO_HEADING).strip()
                     or "_No video context provided._")
        parts.append("")
        parts.append(TRANSCRIPTION_SECTION_H2)
        parts.append("")
        parts.append(_strip_leading_heading(v.transcription, TRANSCRIPTION_HEADING).strip()
                     or "_Not yet generated._")
        parts.append("")
        parts.append(FRAME_ANALYSIS_SECTION_H2)
        parts.append("")
        parts.append(_strip_leading_heading(v.frame_analysis, FRAME_ANALYSIS_HEADING).strip()
                     or "_Not yet generated._")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# --- Parsing the assembled document back into sections ----------------------

def parse_markdown_back(md: str, video_stems: list[tuple[str, str]]) -> dict:
    """Split an assembled markdown document back into sections.

    `video_stems` is a list of (stem, name) pairs in document order — used to
    recognise video block H1s ("# <name>") and map them back to stems.

    Returns:
        {
            "project": str,
            "videos": {stem: {"video": str, "transcription": str, "frame_analysis": str}},
        }

    Defensive: missing or malformed headings produce empty strings rather
    than raising. Content between headings is preserved verbatim (stripped
    of leading/trailing whitespace only).
    """
    result: dict = {
        "project": "",
        "videos": {stem: {"video": "", "transcription": "", "frame_analysis": ""}
                   for stem, _ in video_stems},
    }

    if not md or not md.strip():
        return result

    # Build a lookup of video name -> stem so we can match H1 blocks.
    name_to_stem = {name: stem for stem, name in video_stems}

    # Split into H1 blocks. An H1 is a line starting with "# " (but not "## ").
    blocks = _split_h1_blocks(md)

    for heading, body in blocks:
        h = heading.strip()
        if h == PROJECT_HEADING:
            result["project"] = body.strip()
            continue
        # Video block: "# <name>"
        # Strip the leading "# " from the heading to get the name.
        if h.startswith("# ") and not h.startswith("## "):
            name = h[2:].strip()
            stem = name_to_stem.get(name)
            if stem is None:
                # Try a fuzzy match: maybe the user renamed the heading.
                # Fall back to the first video stem that hasn't been assigned yet.
                for s, n in video_stems:
                    if result["videos"][s]["video"] == "" and \
                       result["videos"][s]["transcription"] == "" and \
                       result["videos"][s]["frame_analysis"] == "":
                        stem = s
                        break
            if stem is not None:
                sections = _split_video_sections(body)
                result["videos"][stem].update(sections)
                continue
    return result


def _split_h1_blocks(md: str) -> list[tuple[str, str]]:
    """Split markdown into (h1_heading, body) pairs.

    An H1 is a line starting with exactly one '# ' (not '## ', '### ', etc.).
    The body is everything until the next H1 or end of document.
    """
    lines = md.splitlines()
    blocks: list[tuple[str, str]] = []
    current_heading: Optional[str] = None
    current_body: list[str] = []

    for line in lines:
        stripped = line.lstrip()
        is_h1 = (stripped.startswith("# ")
                 and not stripped.startswith("## "))
        if is_h1:
            if current_heading is not None:
                blocks.append((current_heading, "\n".join(current_body)))
            current_heading = stripped
            current_body = []
        else:
            if current_heading is not None:
                current_body.append(line)
    if current_heading is not None:
        blocks.append((current_heading, "\n".join(current_body)))
    return blocks


def _split_video_sections(body: str) -> dict:
    """Split a video block body into its 3 H2 sections.

    Returns {"video": str, "transcription": str, "frame_analysis": str}.
    Missing H2s produce empty strings.
    """
    sections = {"video": "", "transcription": "", "frame_analysis": ""}
    current_key: Optional[str] = None
    current_lines: list[str] = []

    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped == VIDEO_SECTION_H2:
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = "video"
            current_lines = []
        elif stripped == TRANSCRIPTION_SECTION_H2:
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = "transcription"
            current_lines = []
        elif stripped == FRAME_ANALYSIS_SECTION_H2:
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = "frame_analysis"
            current_lines = []
        else:
            if current_key is not None:
                current_lines.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


# --- Frame filename extraction from Frame Analysis markdown -----------------

# Matches "## Frame <filename> — <timestamp> (#<index>)" headings produced
# by src/frame_analysis.py:format_section.
_FRAME_HEADING_RE = re.compile(
    r"^##\s+Frame\s+(?P<filename>\S+\.\w+)\s+—",
    re.MULTILINE,
)


def find_frame_filenames_in_frame_analysis(frame_analysis_md: str) -> list[str]:
    """Extract frame filenames from `## Frame <filename> —` headings.

    Returns filenames in document order. Used to build the
    {filename: abs_path} map for inline image rendering.
    """
    if not frame_analysis_md:
        return []
    return [m.group("filename") for m in _FRAME_HEADING_RE.finditer(frame_analysis_md)]


# --- Minimal markdown → HTML converter --------------------------------------

# GFM table detection: a row is a line containing at least one pipe; a table
# starts when a row is immediately followed by a separator row (| --- | --- |).
_TABLE_ROW_RE = re.compile(r"^\s*\|?.*\|.*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _is_table_separator(line: str) -> bool:
    """True if `line` is a GFM table separator row (e.g. '| --- | :---: |')."""
    return bool(_TABLE_SEP_RE.match(line)) and "-" in line


def _split_table_cells(row: str) -> list[str]:
    """Split a GFM table row into cells, honouring escaped pipes (\\|)."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    cells: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def _table_alignments(sep: str) -> list[str]:
    """Derive per-column alignment from a separator row's colons."""
    out: list[str] = []
    for cell in _split_table_cells(sep):
        c = cell.strip()
        if c.startswith(":") and c.endswith(":"):
            out.append("center")
        elif c.endswith(":"):
            out.append("right")
        elif c.startswith(":"):
            out.append("left")
        else:
            out.append("")
    return out


def _cell_style(aligns: list[str], k: int) -> str:
    if k < len(aligns) and aligns[k]:
        return f' style="text-align:{aligns[k]};"'
    return ""


def _render_table(lines: list[str], start: int) -> tuple[str, int]:
    """Render a GFM table whose header row is at `start`.

    Returns (html, next_index) where next_index is the first line after the
    table (a blank line or a non-table line).
    """
    header = _split_table_cells(lines[start])
    aligns = _table_alignments(lines[start + 1])
    body: list[list[str]] = []
    j = start + 2
    while j < len(lines) and _TABLE_ROW_RE.match(lines[j].rstrip()):
        body.append(_split_table_cells(lines[j]))
        j += 1
    parts = ['<table border="1" cellspacing="0" cellpadding="4">', "<thead><tr>"]
    for k, cell in enumerate(header):
        parts.append(f"<th{_cell_style(aligns, k)}>{_inline(cell)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>")
        for k, cell in enumerate(row):
            parts.append(f"<td{_cell_style(aligns, k)}>{_inline(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts), j


def markdown_to_html(md: str, frame_paths_by_filename: Optional[dict] = None
                     ) -> str:
    """Convert the assembled markdown to HTML for QTextBrowser.

    Handles: H1/H2/H3, paragraphs, unordered/ordered lists, GFM tables,
    bold, italic, inline code, and frame image insertion (after
    `## Frame <filename> —` headings in the Frame Analysis section).

    `frame_paths_by_filename` maps frame filename → absolute path on disk.
    When provided and a frame file exists, an <img> tag is inserted after
    the frame heading.
    """
    if not md:
        return ""
    frame_paths = frame_paths_by_filename or {}
    lines = md.splitlines()
    html_parts: list[str] = []
    in_list: Optional[str] = None  # "ul" or "ol"
    para_lines: list[str] = []
    skip_until: int = -1  # lines consumed by a table render

    def flush_paragraph() -> None:
        if para_lines:
            text = " ".join(para_lines).strip()
            if text:
                html_parts.append(f"<p>{_inline(text)}</p>")
            para_lines.clear()

    def flush_list() -> None:
        nonlocal in_list
        if in_list is not None:
            html_parts.append(f"</{in_list}>")
            in_list = None

    for i, line in enumerate(lines):
        if i < skip_until:
            continue
        stripped = line.rstrip()

        # Headings
        if stripped.startswith("### "):
            flush_paragraph()
            flush_list()
            html_parts.append(f"<h3>{_inline(stripped[4:].strip())}</h3>")
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            flush_list()
            heading_text = stripped[3:].strip()
            html_parts.append(f"<h2>{_inline(heading_text)}</h2>")
            # Frame image insertion: if this is a "## Frame <filename> —" heading
            m = _FRAME_HEADING_RE.match(stripped)
            if m:
                filename = m.group("filename")
                abs_path = frame_paths.get(filename)
                if abs_path and Path(abs_path).exists():
                    url = Path(abs_path).as_uri()
                    html_parts.append(
                        f'<img src="{url}" '
                        f'style="max-width:50%; '
                        f'border-radius:4px; margin:8px 0; '
                        f'border:1px solid #252D3D;">'
                    )
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            flush_list()
            html_parts.append(f"<h1>{_inline(stripped[2:].strip())}</h1>")
            continue

        # GFM table: a row followed by a separator row.
        if _TABLE_ROW_RE.match(stripped) and i + 1 < len(lines) \
                and _is_table_separator(lines[i + 1].rstrip()):
            flush_paragraph()
            flush_list()
            table_html, skip_until = _render_table(lines, i)
            html_parts.append(table_html)
            continue

        # Lists
        ul_match = re.match(r"^\s*[-*]\s+(.+)$", stripped)
        ol_match = re.match(r"^\s*\d+\.\s+(.+)$", stripped)
        if ul_match:
            flush_paragraph()
            if in_list != "ul":
                flush_list()
                html_parts.append("<ul>")
                in_list = "ul"
            html_parts.append(f"<li>{_inline(ul_match.group(1))}</li>")
            continue
        if ol_match:
            flush_paragraph()
            if in_list != "ol":
                flush_list()
                html_parts.append("<ol>")
                in_list = "ol"
            html_parts.append(f"<li>{_inline(ol_match.group(1))}</li>")
            continue

        # Blank line → paragraph break
        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        # Default: accumulate into the current paragraph
        para_lines.append(stripped)

    flush_paragraph()
    flush_list()
    return "\n".join(html_parts)


def _inline(text: str) -> str:
    """Convert inline markdown (bold, italic, code) to HTML."""
    # Escape HTML special chars first
    out = (text.replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;"))
    # Inline code: `code`
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    # Bold: **text**
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    # Italic: *text* (but not ** which is bold)
    out = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    # Markdown image: ![alt](path) → <img>
    img_match = re.match(r"^\!\[(.*?)\]\((.+?)\)$", out.strip())
    if img_match:
        alt = img_match.group(1)
        path = img_match.group(2)
        # Convert to file:// URI if it's an absolute local path.
        if not path.startswith(("http://", "https://", "file://")):
            p = Path(path)
            if p.is_absolute():
                path = p.as_uri()
            # Relative paths are left as-is; QTextBrowser resolves them
            # relative to the document's search path (set via setSearchPaths).
        return (f'<img src="{path}" alt="{alt}" '
                f'style="max-width:50%; border-radius:4px; '
                f'border:1px solid #252D3D;">')
    return out


# --- Export markdown --------------------------------------------------------

def build_export_markdown(doc: AssembledDocument,
                          frame_paths_by_filename: Optional[dict] = None) -> str:
    """Build the pure-markdown export with image links.

    Produces a single markdown document viewable in any markdown reader.
    Frame references in the Frame Analysis section become
    `![frame](<abs_path>)` image links.
    """
    frame_paths = frame_paths_by_filename or {}
    parts: list[str] = ["# Video Context Report", ""]

    # Project context
    parts.append(PROJECT_HEADING)
    parts.append("")
    project_body = _strip_leading_heading(doc.project_context, PROJECT_HEADING)
    parts.append(project_body.strip() if project_body.strip()
                 else "_No project context provided._")
    parts.append("")

    for v in doc.videos:
        parts.append(video_block_heading(v.name))
        parts.append("")
        if v.thumbnail_path and Path(v.thumbnail_path).exists():
            parts.append(f"![thumbnail]({Path(v.thumbnail_path).as_posix()})")
            parts.append("")

        # Video context
        parts.append(VIDEO_SECTION_H2)
        parts.append("")
        body = _strip_leading_heading(v.video_context, VIDEO_HEADING).strip()
        parts.append(body or "_No video context provided._")
        parts.append("")

        # Transcription
        parts.append(TRANSCRIPTION_SECTION_H2)
        parts.append("")
        body = _strip_leading_heading(v.transcription, TRANSCRIPTION_HEADING).strip()
        parts.append(body or "_Not yet generated._")
        parts.append("")

        # Frame analysis — inject frame images after each frame heading
        parts.append(FRAME_ANALYSIS_SECTION_H2)
        parts.append("")
        fa_body = _strip_leading_heading(v.frame_analysis, FRAME_ANALYSIS_HEADING).strip()
        if fa_body:
            parts.append(_inject_frame_images(fa_body, frame_paths))
        else:
            parts.append("_Not yet generated._")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _inject_frame_images(frame_analysis_md: str,
                         frame_paths_by_filename: dict) -> str:
    """Insert `![frame](path)` image links after each `## Frame <filename> —` heading.

    This produces the timeline-of-frames visual in the exported markdown.
    """
    if not frame_analysis_md:
        return frame_analysis_md

    def _replace(match: re.Match) -> str:
        heading = match.group(0)
        filename = match.group("filename")
        abs_path = frame_paths_by_filename.get(filename)
        if abs_path and Path(abs_path).exists():
            img_line = f"\n\n![{filename}]({Path(abs_path).as_posix()})"
            return heading + img_line
        return heading

    return _FRAME_HEADING_RE.sub(_replace, frame_analysis_md)


# --- Split assembled document into per-section strings (for edit mode) ------

def split_sections_for_edit(md: str, video_stems: list[tuple[str, str]]
                            ) -> dict:
    """Split an assembled document into per-section strings for editing.

    Returns a dict keyed:
        "project" -> str
        "<stem>::video" -> str
        "<stem>::transcription" -> str
        "<stem>::frame_analysis" -> str

    Each value is the raw markdown for that section (without the H1/H2 heading).
    """
    parsed = parse_markdown_back(md, video_stems)
    out: dict = {"project": parsed["project"]}
    for stem, _name in video_stems:
        v = parsed["videos"].get(stem, {})
        out[f"{stem}::video"] = v.get("video", "")
        out[f"{stem}::transcription"] = v.get("transcription", "")
        out[f"{stem}::frame_analysis"] = v.get("frame_analysis", "")
    return out


# --- Helpers ----------------------------------------------------------------

def _strip_leading_heading(content: str, heading: str) -> str:
    """If `content` starts with `heading` (possibly with trailing whitespace),
    strip that first line. Otherwise return content unchanged.
    """
    if not content:
        return content
    lines = content.splitlines()
    if lines and lines[0].strip() == heading:
        return "\n".join(lines[1:])
    return content