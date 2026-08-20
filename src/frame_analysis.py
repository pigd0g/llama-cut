from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Optional, Tuple


# --- Prompt template --------------------------------------------------------

FRAME_ANALYSIS_PROMPT = """You are analysing a single frame extracted from a video.

Your task is to describe what is visually present in the frame accurately and objectively. Use the supplied project and video context to understand the significance of what you see, but **do not invent details that are not visible in the image**.

## Project Context

{{PROJECT_CONTEXT}}

## Video Context

{{VIDEO_CONTEXT}}

## Frame Information

- Source video: {{VIDEO_FILENAME}}
- Timestamp: {{TIMESTAMP}}
- Frame number: {{FRAME_NUMBER}}

## Instructions

Analyse the provided image and describe:

1. **Scene** — What is happening and where the scene appears to take place.
2. **People** — Visible people, their approximate position, actions, clothing, and notable characteristics. Do not identify people by name unless the context explicitly establishes their identity and the visual evidence is consistent.
3. **Objects** — Important objects, vehicles, equipment, animals, or other notable items.
4. **Actions** — What people, animals, vehicles, or objects appear to be doing.
5. **Environment** — Relevant background, setting, weather, lighting, and surroundings.
6. **Text** — Any visible signs, labels, screens, captions, or other readable text. Transcribe only text that can actually be read.
7. **Notable Details** — Anything visually significant that may be useful for understanding, editing, storytelling, or subsequent video analysis.
8. **Uncertainty** — Clearly distinguish observations from assumptions. If something cannot be determined from the frame, say so.

### Important Rules

- Describe only what can reasonably be inferred from the image.
- Do not hallucinate details.
- Do not assume that something mentioned in the context is visible in this particular frame.
- Use the context to provide relevance, not to manufacture visual information.
- If the frame contains something potentially important given the project/video context, explain why it appears relevant.
- If the image quality prevents reliable identification of a detail, explicitly state that.
- Be concise but sufficiently descriptive to make the frame understandable without seeing it.

Return a structured analysis using this format:

Scene:
...

People:
...

Objects:
...

Actions:
...

Environment:
...

Visible Text:
...

Notable Details:
...

Uncertainty:
...
"""

_EMPTY_PROJECT = "_No project context provided._"
_EMPTY_VIDEO = "_No video context provided._"


# --- Ollama configuration ----------------------------------------------------

@dataclass
class OllamaConfig:
    host: str
    api_key: str
    model: str


def load_ollama_config() -> OllamaConfig:
    """Read Ollama endpoint config from environment variables.

    Does NOT call load_dotenv() itself — main.py does that at startup so the
    .env values are present before this function is ever called.
    """
    return OllamaConfig(
        host=os.environ.get("OLLAMA_HOST", "").strip(),
        api_key=os.environ.get("OLLAMA_API_KEY", "").strip(),
        model=os.environ.get("OLLAMA_VISION_MODEL", "").strip(),
    )


def is_config_valid(config: Optional[OllamaConfig] = None) -> Tuple[bool, str]:
    """Return (ok, message). message is "" on success, a human-readable hint otherwise."""
    cfg = config if config is not None else load_ollama_config()
    missing = []
    if not cfg.host:
        missing.append("OLLAMA_HOST")
    if not cfg.model:
        missing.append("OLLAMA_VISION_MODEL")
    if missing:
        return False, (
            "Ollama configuration is missing or incomplete. "
            f"Set {', '.join(missing)} in your .env file."
        )
    # api_key is optional for local Ollama but required for cloud; do not block.
    return True, ""


def build_ollama_client(config: OllamaConfig):
    """Construct an ollama.Client with host + optional bearer auth.

    The auth header is only added when api_key is non-empty so local Ollama
    (no key) keeps working.
    """
    from ollama import Client

    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return Client(host=config.host, headers=headers or None)


# --- Settings ---------------------------------------------------------------

@dataclass
class FrameAnalysisSettings:
    concurrency: int = 3

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FrameAnalysisSettings":
        fields = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


# --- Prompt construction ----------------------------------------------------

def build_prompt(project_ctx: str, video_ctx: str, video_filename: str,
                 timestamp: str, frame_number: int) -> str:
    """Substitute the five placeholders in FRAME_ANALYSIS_PROMPT.

    Empty project/video context is replaced with a placeholder message so the
    prompt stays well-formed for the model.
    """
    p = project_ctx.strip() if project_ctx and project_ctx.strip() else _EMPTY_PROJECT
    v = video_ctx.strip() if video_ctx and video_ctx.strip() else _EMPTY_VIDEO
    out = FRAME_ANALYSIS_PROMPT
    out = out.replace("{{PROJECT_CONTEXT}}", p)
    out = out.replace("{{VIDEO_CONTEXT}}", v)
    out = out.replace("{{VIDEO_FILENAME}}", video_filename)
    out = out.replace("{{TIMESTAMP}}", timestamp)
    out = out.replace("{{FRAME_NUMBER}}", str(frame_number))
    return out


# --- Timestamp + section formatting ----------------------------------------

def format_timestamp_hms(pts_time: float) -> str:
    """Return HH:MM:SS.mmm (readable variant). 0-padded; uses dot before ms.

    Distinct from src/ffmpeg/timestamp.py:format_timestamp which uses dashes
    for filename safety.
    """
    if pts_time < 0:
        pts_time = 0.0
    total_ms = int(round(pts_time * 1000.0))
    hours = total_ms // 3_600_000
    remaining = total_ms % 3_600_000
    minutes = remaining // 60_000
    remaining %= 60_000
    seconds = remaining // 1000
    ms = remaining % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def format_section(frame, llm_text: str) -> str:
    """Build the markdown section for one frame's analysis.

    `frame` is a src.state.Frame instance (must have filename, pts_time, index).
    """
    ts = format_timestamp_hms(frame.pts_time)
    header = f"## Frame {frame.filename} — {ts} (#{frame.index})"
    stripped = (llm_text or "").strip()
    body = stripped if stripped else "_(no response)_"
    return f"{header}\n\n{body}"


def append_sections(existing_content: str, run_timestamp: str,
                     new_sections: list[str]) -> str:
    """Join existing content + a `## Run — <ts>` header + new sections.

    Handles missing trailing newlines on existing content and empty existing
    content (no leading blank line).
    """
    parts: list[str] = []
    if existing_content and existing_content.strip():
        parts.append(existing_content.rstrip())
    parts.append(f"## Run — {run_timestamp}")
    for sec in new_sections:
        parts.append(sec.rstrip())
    return "\n\n".join(parts) + "\n"


# --- Frame analysis call ----------------------------------------------------

def analyse_frame(client, model: str, frame_path: str, project_ctx: str,
                  video_ctx: str, video_filename: str, frame) -> str:
    """Send one frame to the Ollama vision model and return the response text.

    `frame` is a src.state.Frame (for timestamp + index). Uses temperature=0
    for deterministic extraction-style output, per the ollama skill guidance.
    Raises ollama.ResponseError on failure; the caller handles it.
    """
    prompt = build_prompt(
        project_ctx=project_ctx,
        video_ctx=video_ctx,
        video_filename=video_filename,
        timestamp=format_timestamp_hms(frame.pts_time),
        frame_number=frame.index,
    )
    response = client.chat(
        model=model,
        messages=[{
            "role": "user",
            "content": prompt,
            "images": [frame_path],
        }],
        options={"temperature": 0},
    )
    return getattr(response.message, "content", "") or ""