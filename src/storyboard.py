from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple


# --- Storyboard directory / file names --------------------------------------

STORYBOARD_DIR = "storyboard"
HISTORY_FILENAME = "history.json"
LATEST_FILENAME = "storyboard.md"
EXPORT_FILENAME = "storyboard_export.md"


# --- System prompt ----------------------------------------------------------

STORYBOARD_SYSTEM_PROMPT = """You are a creative video agency responsible for delivering a storyboard for a high-quality video based on the user's brief.

You can do things such as:

- Turning long recordings into short-form content
- Building vlogs, tutorials, or demo videos from raw capture
- Adding overlays, subtitles, music, or voiceover to existing video

Only add a music file for background if the user explicitly asks for music.

Develop a detailed storyboard for the video.

If the user has provided a concept for the video, flesh it out into a more detailed creative plan that achieves their goal.

Make use of all provided context to understand the existing video content.

The context may include:

- Project Context — information about the overall project and what the video should achieve
- Per Video Context — information about individual source videos
- Per Video Metadata — technical information about each source video, including resolution, frame rate, duration, codec, aspect ratio and audio properties
- Per Video Transcription — speech and dialogue from the source videos
- Per Video Frame Analysis — visual information about specific frames, including timestamps and notable moments

Use this context to develop the storyboard.

Use the technical metadata when making production decisions. For example, consider source resolution, frame rate, aspect ratio and duration when recommending how footage should be used.

Identify and use the best available moments from the source footage, including:

- Strong opening hooks
- Interesting or visually compelling moments
- Important events
- Useful dialogue
- Emotional moments
- Funny or entertaining moments
- Good establishing shots
- Relevant B-roll
- Moments that support the intended story

Where possible, reference the source video and timestamp when recommending footage.

Do not invent footage, dialogue, events, people, locations, or visual content that is not supported by the provided context.
Do not intentionally overlap or repeat footage unless the context indicates that it is appropriate and the user specifies that it is desired.

If the available footage does not contain something required by the user's concept, clearly identify the gap rather than inventing a clip.

Consider:

- Overall story structure
- Opening hook
- Narrative progression
- Pacing
- Shot selection
- B-roll
- Dialogue
- Voiceover
- On-screen text
- Subtitles
- Transitions
- Visual treatment
- Ending
- Approximate duration
- Source video technical characteristics

Only recommend background music if the user explicitly requests it.

If important information about the desired video format, design, content, audience, duration, platform, or creative direction is genuinely missing and prevents you from producing a good storyboard, ask the user a concise question.

Otherwise, make sensible creative decisions based on the user's brief and the available context.
"""


REFINEMENT_INSTRUCTIONS = """You are refining an existing storyboard.

Preserve good decisions from the existing storyboard unless the user's new instruction requires them to change.

Apply the user's requested changes.

Use the supplied project, video, metadata, transcription and frame analysis context to validate and improve the storyboard.

Do not invent footage that is not supported by the context.

Return the complete revised storyboard, not only the changes.
"""


# --- Ollama configuration ----------------------------------------------------

@dataclass
class StoryboardSettings:
    """Persisted per-project storyboard UI settings.

    Minimal for now — just the last-used creative brief so the user doesn't
    lose their text when navigating away and back. Extensible in the future
    (e.g. default temperature, output format preferences).
    """
    last_brief: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StoryboardSettings":
        fields = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass
class StoryboardConfig:
    host: str
    api_key: str
    model: str


def load_storyboard_config() -> StoryboardConfig:
    """Read Ollama endpoint config for the storyboard workflow from env vars.

    Uses ``OLLAMA_WORKFLOW_MODEL`` for the model name (distinct from
    ``OLLAMA_VISION_MODEL`` used by frame analysis). Host and API key are
    shared with the vision config.

    Does NOT call ``load_dotenv()`` — ``main.py`` does that at startup.
    """
    return StoryboardConfig(
        host=os.environ.get("OLLAMA_HOST", "").strip(),
        api_key=os.environ.get("OLLAMA_API_KEY", "").strip(),
        model=os.environ.get("OLLAMA_WORKFLOW_MODEL", "").strip(),
    )


def is_config_valid(config: Optional[StoryboardConfig] = None
                    ) -> Tuple[bool, str]:
    """Return (ok, message). message is "" on success, a human-readable hint otherwise."""
    cfg = config if config is not None else load_storyboard_config()
    missing = []
    if not cfg.host:
        missing.append("OLLAMA_HOST")
    if not cfg.model:
        missing.append("OLLAMA_WORKFLOW_MODEL")
    if missing:
        return False, (
            "Ollama configuration is missing or incomplete. "
            f"Set {', '.join(missing)} in your .env file."
        )
    return True, ""


def build_ollama_client(config: StoryboardConfig):
    """Construct an ollama.Client with host + optional bearer auth."""
    from ollama import Client

    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return Client(host=config.host, headers=headers or None)


# --- Context assembly -------------------------------------------------------

_EMPTY_PROJECT = "_No project context provided._"
_EMPTY_VIDEO = "_No video context provided._"
_EMPTY_TRANSCRIPTION = "_Not yet generated._"
_EMPTY_FRAME_ANALYSIS = "_Not yet generated._"


def build_context_markdown(project_ctx: str,
                            video_sections: list,
                            video_metadatas: list) -> str:
    """Assemble all available context into a single structured Markdown document.

    ``video_sections`` is a list of ``context_review.VideoSection`` objects
    (from ``load_assembled``). ``video_metadatas`` is a list of
    ``VideoMetadata`` objects.

    The structure is:

      # Project Context
      <project content>

      # Video Metadata
      ## <video1 filename>
      - Duration: ...
      ...

      # Per-Video Context

      ## <video1 name>
      ### Video Context
      <content>
      ### Transcription
      <content>
      ### Frame Analysis
      <content>

      ## <video2 name>
      ...
    """
    from .context_review import _strip_leading_heading, VIDEO_HEADING
    from .video_metadata import metadata_to_markdown_all

    parts: list[str] = []

    # --- Project Context ---
    parts.append("# Project Context")
    parts.append("")
    pbody = _strip_leading_heading(project_ctx, "# Project Context").strip()
    parts.append(pbody if pbody else _EMPTY_PROJECT)
    parts.append("")

    # --- Video Metadata ---
    if video_metadatas:
        parts.append(metadata_to_markdown_all(video_metadatas).rstrip())
        parts.append("")

    # --- Per-Video Context ---
    parts.append("# Per-Video Context")
    parts.append("")
    for v in video_sections:
        parts.append(f"## {v.name}")
        parts.append("")
        parts.append(f"- Source filename: `{v.name}`")
        parts.append(f"- Source stem: `{v.stem}`")
        if v.thumbnail_path:
            parts.append(f"- Thumbnail: `{v.thumbnail_path}`")
        parts.append("")
        parts.append("### Video Context")
        parts.append("")
        vc = _strip_leading_heading(v.video_context, VIDEO_HEADING).strip()
        parts.append(vc if vc else _EMPTY_VIDEO)
        parts.append("")
        parts.append("### Transcription")
        parts.append("")
        tc = v.transcription.strip()
        parts.append(tc if tc else _EMPTY_TRANSCRIPTION)
        parts.append("")
        parts.append("### Frame Analysis")
        parts.append("")
        fa = v.frame_analysis.strip()
        parts.append(fa if fa else _EMPTY_FRAME_ANALYSIS)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# --- Prompt building --------------------------------------------------------

def build_generation_prompt(brief: str, context_md: str) -> str:
    """Build the user-message content for an initial storyboard generation.

    The system prompt (STORYBOARD_SYSTEM_PROMPT) is sent as a separate
    ``system`` role message in the chat call; this function returns only the
    user-role content.
    """
    return (
        f"## Creative Brief\n\n{brief.strip()}\n\n"
        f"## Available Context\n\n{context_md.strip()}\n\n"
        f"## Task\n\n"
        f"Develop a detailed storyboard based on the creative brief and the "
        f"available context. Follow the structure described in your "
        f"instructions. Reference source videos and timestamps where the "
        f"context supports them. Do not invent footage that is not in the "
        f"context. Return the complete storyboard as Markdown."
    )


def build_refinement_prompt(new_prompt: str, existing_storyboard: str,
                            context_md: str) -> str:
    """Build the user-message content for a storyboard refinement.

    Includes the existing storyboard, the user's new instruction, and the
    full context so the LLM can validate and improve source selections.
    The refinement instructions (REFINEMENT_INSTRUCTIONS) are included here
    so the model treats the new prompt as a refinement, not a fresh start.
    """
    return (
        f"{REFINEMENT_INSTRUCTIONS.strip()}\n\n"
        f"## Existing Storyboard\n\n{existing_storyboard.strip()}\n\n"
        f"## User's New Instruction\n\n{new_prompt.strip()}\n\n"
        f"## Available Context\n\n{context_md.strip()}\n\n"
        f"## Task\n\n"
        f"Apply the user's new instruction to refine the existing storyboard. "
        f"Preserve good decisions from the existing storyboard unless the "
        f"user's instruction requires them to change. Return the complete "
        f"revised storyboard as Markdown."
    )


# --- LLM calls --------------------------------------------------------------

def generate_storyboard(client, model: str, prompt: str) -> str:
    """Send the generation request to Ollama and return the response text.

    Uses ``temperature=0.7`` for creative output (higher than frame analysis's
    deterministic 0). The system prompt is sent as a separate ``system`` message.
    """
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": STORYBOARD_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        options={"temperature": 0.7},
    )
    return getattr(response.message, "content", "") or ""


def refine_storyboard(client, model: str, prompt: str) -> str:
    """Send a refinement request to Ollama and return the response text.

    Uses the same system prompt as generation; the refinement instructions
    are embedded in the user message via ``build_refinement_prompt``.
    """
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": STORYBOARD_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        options={"temperature": 0.7},
    )
    return getattr(response.message, "content", "") or ""


# --- Versioning -------------------------------------------------------------

@dataclass
class StoryboardVersion:
    version: int
    prompt: str           # the user's brief/refinement prompt
    timestamp: str        # ISO timestamp
    storyboard: str       # the full markdown
    model: str            # model name used
    is_initial: bool      # True for v1 (generation), False for refinements
    updated: str = ""     # ISO timestamp of last manual edit (empty if never)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StoryboardVersion":
        fields = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass
class StoryboardHistory:
    versions: list[StoryboardVersion] = field(default_factory=list)

    @property
    def latest(self) -> Optional[StoryboardVersion]:
        """Return the most recent version, or None if history is empty."""
        if not self.versions:
            return None
        return self.versions[-1]

    def add(self, prompt: str, storyboard: str, model: str,
            is_initial: bool) -> StoryboardVersion:
        """Append a new version and return it."""
        version_num = len(self.versions) + 1
        v = StoryboardVersion(
            version=version_num,
            prompt=prompt,
            timestamp=_now_iso(),
            storyboard=storyboard,
            model=model,
            is_initial=is_initial,
        )
        self.versions.append(v)
        return v

    def update_latest(self, storyboard: str) -> None:
        """Update the latest version's storyboard text in-place (manual edit)."""
        latest = self.latest
        if latest is not None:
            latest.storyboard = storyboard
            latest.updated = _now_iso()

    def to_dict(self) -> dict:
        return {"versions": [v.to_dict() for v in self.versions]}

    @classmethod
    def from_dict(cls, d: dict) -> "StoryboardHistory":
        versions = [StoryboardVersion.from_dict(v) for v in d.get("versions", [])]
        return cls(versions=versions)


# --- Persistence ------------------------------------------------------------

def _storyboard_dir(working_folder: str) -> Path:
    return Path(working_folder) / STORYBOARD_DIR


def load_history(working_folder: str) -> StoryboardHistory:
    """Load storyboard history from ``<working_folder>/storyboard/history.json``.

    Returns an empty StoryboardHistory if the file does not exist or is
    corrupt (never raises).
    """
    p = _storyboard_dir(working_folder) / HISTORY_FILENAME
    if not p.exists():
        return StoryboardHistory()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return StoryboardHistory.from_dict(data)
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return StoryboardHistory()


def save_history(working_folder: str, history: StoryboardHistory) -> None:
    """Persist storyboard history to ``<working_folder>/storyboard/history.json``."""
    d = _storyboard_dir(working_folder)
    d.mkdir(parents=True, exist_ok=True)
    (d / HISTORY_FILENAME).write_text(
        json.dumps(history.to_dict(), indent=2), encoding="utf-8",
    )


def save_latest_storyboard(working_folder: str, storyboard: str) -> None:
    """Write the latest storyboard markdown to ``<working_folder>/storyboard/storyboard.md``."""
    d = _storyboard_dir(working_folder)
    d.mkdir(parents=True, exist_ok=True)
    (d / LATEST_FILENAME).write_text(storyboard, encoding="utf-8")


def load_latest_storyboard(working_folder: str) -> str:
    """Read the latest storyboard markdown. Returns "" if the file does not exist."""
    p = _storyboard_dir(working_folder) / LATEST_FILENAME
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def export_storyboard(working_folder: str, storyboard: str) -> Path:
    """Write the storyboard to ``<working_folder>/storyboard/storyboard_export.md``.

    Returns the path to the written file.
    """
    d = _storyboard_dir(working_folder)
    d.mkdir(parents=True, exist_ok=True)
    p = d / EXPORT_FILENAME
    p.write_text(storyboard, encoding="utf-8")
    return p


def clear_storyboard(working_folder: str) -> None:
    """Delete all storyboard artefacts (history, latest, export).

    Removes the entire ``<working_folder>/storyboard/`` directory so that
    no iterations or previous storyboard artefacts are preserved. This is
    a clean-slate action. Does not raise if the directory does not exist.
    """
    d = _storyboard_dir(working_folder)
    if not d.exists():
        return
    import shutil
    shutil.rmtree(d, ignore_errors=True)


# --- Helpers ----------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")