from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.storyboard import (
    STORYBOARD_SYSTEM_PROMPT,
    REFINEMENT_INSTRUCTIONS,
    HISTORY_FILENAME,
    LATEST_FILENAME,
    EXPORT_FILENAME,
    StoryboardConfig,
    StoryboardHistory,
    StoryboardSettings,
    StoryboardVersion,
    build_context_markdown,
    build_generation_prompt,
    build_ollama_client,
    build_refinement_prompt,
    clear_storyboard,
    export_storyboard,
    generate_storyboard,
    is_config_valid,
    load_history,
    load_latest_storyboard,
    load_storyboard_config,
    refine_storyboard,
    save_history,
    save_latest_storyboard,
)
from src import paths
from src.video_metadata import VideoMetadata


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture
def tmp_working(tmp_path: Path) -> Path:
    """A temporary working folder with a .llama-cut/storyboard/ subdirectory."""
    paths.storyboard_dir(str(tmp_path)).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_video_section(name="ClipA.mp4", stem="ClipA"):
    """Build a VideoSection-like object for context assembly tests."""
    from src.context_review import VideoSection
    return VideoSection(
        stem=stem,
        name=name,
        thumbnail_path="",
        video_context="# Video Context\n\nA test video.",
        transcription="# Transcription\n\nHello world.",
        frame_analysis="# Frame Analysis\n\n## Run — ts\n\nScene: test.",
    )


def _make_metadata(filename="ClipA.mp4") -> VideoMetadata:
    return VideoMetadata(
        source_filename=filename,
        source_path=f"/videos/{filename}",
        duration=60.0,
        duration_hms="00:01:00",
        video_codec="h264",
        width=1920,
        height=1080,
        frame_rate=25.0,
        pixel_format="yuv420p",
        aspect_ratio="16:9",
        num_video_streams=1,
        audio_codec="aac",
        audio_sample_rate="48000",
        audio_channels=2,
        audio_channel_layout="stereo",
        num_audio_streams=1,
    )


# --- StoryboardConfig + env loading -----------------------------------------

def test_load_storyboard_config_reads_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    monkeypatch.setenv("OLLAMA_WORKFLOW_MODEL", "gemma4:31b")
    cfg = load_storyboard_config()
    assert cfg == StoryboardConfig("http://localhost:11434", "secret", "gemma4:31b")


def test_load_storyboard_config_strips_whitespace(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "  http://localhost  ")
    monkeypatch.setenv("OLLAMA_API_KEY", "  key  ")
    monkeypatch.setenv("OLLAMA_WORKFLOW_MODEL", "  gemma4  ")
    cfg = load_storyboard_config()
    assert cfg.host == "http://localhost"
    assert cfg.api_key == "key"
    assert cfg.model == "gemma4"


def test_load_storyboard_config_missing_vars(monkeypatch):
    for v in ("OLLAMA_HOST", "OLLAMA_API_KEY", "OLLAMA_WORKFLOW_MODEL"):
        monkeypatch.delenv(v, raising=False)
    cfg = load_storyboard_config()
    assert cfg.host == ""
    assert cfg.api_key == ""
    assert cfg.model == ""


def test_load_storyboard_config_uses_workflow_model_not_vision(monkeypatch):
    """Ensure OLLAMA_WORKFLOW_MODEL is used, not OLLAMA_VISION_MODEL."""
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost")
    monkeypatch.setenv("OLLAMA_API_KEY", "")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "vision_model")
    monkeypatch.setenv("OLLAMA_WORKFLOW_MODEL", "workflow_model")
    cfg = load_storyboard_config()
    assert cfg.model == "workflow_model"


# --- is_config_valid --------------------------------------------------------

def test_is_config_valid_ok():
    cfg = StoryboardConfig("http://localhost:11434", "", "gemma4")
    ok, msg = is_config_valid(cfg)
    assert ok is True
    assert msg == ""


def test_is_config_valid_ok_with_api_key():
    cfg = StoryboardConfig("https://ollama.com", "key", "gemma4")
    ok, _ = is_config_valid(cfg)
    assert ok is True


def test_is_config_valid_missing_host():
    cfg = StoryboardConfig("", "key", "gemma4")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_HOST" in msg


def test_is_config_valid_missing_model():
    cfg = StoryboardConfig("http://localhost", "", "")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_WORKFLOW_MODEL" in msg


def test_is_config_valid_missing_both():
    cfg = StoryboardConfig("", "", "")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_HOST" in msg
    assert "OLLAMA_WORKFLOW_MODEL" in msg


# --- build_ollama_client ----------------------------------------------------

def test_build_ollama_client_with_api_key():
    cfg = StoryboardConfig("http://localhost:11434", "secret", "gemma4")
    with patch("ollama.Client") as mock_cls:
        build_ollama_client(cfg)
        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        assert kwargs["host"] == "http://localhost:11434"
        assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_build_ollama_client_without_api_key():
    cfg = StoryboardConfig("http://localhost:11434", "", "gemma4")
    with patch("ollama.Client") as mock_cls:
        build_ollama_client(cfg)
        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        assert kwargs["headers"] is None


# --- build_context_markdown -------------------------------------------------

def test_build_context_markdown_structure():
    project_ctx = "# Project Context\n\nA family holiday project."
    videos = [_make_video_section()]
    metas = [_make_metadata()]
    md = build_context_markdown(project_ctx, videos, metas)
    # Project context
    assert "# Project Context" in md
    assert "A family holiday project." in md
    # Video metadata
    assert "# Video Metadata" in md
    assert "## ClipA.mp4" in md
    assert "h264" in md
    assert "1920 × 1080" in md
    # Per-video context
    assert "# Per-Video Context" in md
    assert "## ClipA.mp4" in md  # video name
    assert "### Video Context" in md
    assert "A test video." in md
    assert "### Transcription" in md
    assert "Hello world." in md
    assert "### Frame Analysis" in md
    assert "Scene: test." in md


def test_build_context_markdown_empty_project():
    videos = [_make_video_section()]
    metas = [_make_metadata()]
    md = build_context_markdown("", videos, metas)
    assert "_No project context provided._" in md


def test_build_context_markdown_no_metadata():
    project_ctx = "# Project Context\n\nProject."
    videos = [_make_video_section()]
    md = build_context_markdown(project_ctx, videos, [])
    # No video metadata section should appear
    assert "# Video Metadata" not in md
    # But per-video context should still be there
    assert "# Per-Video Context" in md


def test_build_context_markdown_empty_sections():
    from src.context_review import VideoSection
    v = VideoSection(stem="X", name="X.mp4", thumbnail_path="")
    md = build_context_markdown("", [v], [])
    assert "_No video context provided._" in md
    assert "_Not yet generated._" in md  # transcription placeholder
    # Frame analysis placeholder also uses _Not yet generated._
    assert md.count("_Not yet generated._") >= 2


def test_build_context_markdown_multiple_videos():
    from src.context_review import VideoSection
    v1 = VideoSection(stem="A", name="A.mp4", thumbnail_path="", video_context="Ctx A")
    v2 = VideoSection(stem="B", name="B.mp4", thumbnail_path="", video_context="Ctx B")
    md = build_context_markdown("", [v1, v2], [])
    assert "## A.mp4" in md
    assert "## B.mp4" in md
    assert "Ctx A" in md
    assert "Ctx B" in md


# --- Prompt building --------------------------------------------------------

def test_build_generation_prompt_includes_brief():
    md = build_generation_prompt("Make a vlog", "# Context\n\nSome context")
    assert "Make a vlog" in md
    assert "Some context" in md
    assert "Creative Brief" in md
    assert "Available Context" in md


def test_build_refinement_prompt_includes_existing_storyboard():
    existing = "# Storyboard\n\n## Scene 1\n\nOld content."
    md = build_refinement_prompt("Make it longer", existing, "# Context\n\nCtx")
    assert "Make it longer" in md
    assert "Old content." in md
    assert "Ctx" in md
    assert "Existing Storyboard" in md
    # Should include refinement instructions
    assert "Preserve good decisions" in md


def test_system_prompt_contains_key_instructions():
    # Music only when requested
    assert "music" in STORYBOARD_SYSTEM_PROMPT.lower()
    assert "user explicitly" in STORYBOARD_SYSTEM_PROMPT.lower()
    # Don't invent footage
    assert "Do not invent" in STORYBOARD_SYSTEM_PROMPT
    # Use technical metadata
    assert "technical metadata" in STORYBOARD_SYSTEM_PROMPT.lower()
    # Reference source video and timestamp
    assert "reference the source video" in STORYBOARD_SYSTEM_PROMPT.lower()
    assert "timestamp" in STORYBOARD_SYSTEM_PROMPT.lower()


def test_refinement_instructions_key_content():
    assert "Preserve good decisions" in REFINEMENT_INSTRUCTIONS
    assert "Do not invent" in REFINEMENT_INSTRUCTIONS
    assert "complete revised storyboard" in REFINEMENT_INSTRUCTIONS.lower()


# --- generate_storyboard / refine_storyboard (mocked) ----------------------

def test_generate_storyboard_calls_client():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.message.content = "# Storyboard\n\n## Scene 1"
    mock_client.chat.return_value = mock_response
    result = generate_storyboard(mock_client, "gemma4", "test prompt")
    mock_client.chat.assert_called_once()
    _, kwargs = mock_client.chat.call_args
    assert kwargs["model"] == "gemma4"
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["role"] == "user"
    assert kwargs["messages"][0]["content"] == STORYBOARD_SYSTEM_PROMPT
    assert kwargs["options"]["temperature"] == 0.7
    assert result == "# Storyboard\n\n## Scene 1"


def test_generate_storyboard_empty_response():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.message.content = ""
    mock_client.chat.return_value = mock_response
    result = generate_storyboard(mock_client, "m", "prompt")
    assert result == ""


def test_refine_storyboard_calls_client():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.message.content = "# Refined Storyboard"
    mock_client.chat.return_value = mock_response
    result = refine_storyboard(mock_client, "m", "refine prompt")
    mock_client.chat.assert_called_once()
    _, kwargs = mock_client.chat.call_args
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["role"] == "user"
    assert kwargs["options"]["temperature"] == 0.7
    assert result == "# Refined Storyboard"


# --- StoryboardVersion / StoryboardHistory ---------------------------------

def test_storyboard_version_to_dict_roundtrip():
    v = StoryboardVersion(
        version=1, prompt="test", timestamp="2025-01-01T00:00:00",
        storyboard="# SB", model="m", is_initial=True,
    )
    d = v.to_dict()
    assert d["version"] == 1
    assert d["prompt"] == "test"
    assert d["is_initial"] is True
    v2 = StoryboardVersion.from_dict(d)
    assert v2 == v


def test_storyboard_version_from_dict_ignores_extra_keys():
    d = {"version": 1, "prompt": "p", "timestamp": "ts", "storyboard": "sb",
         "model": "m", "is_initial": True, "updated": "", "extra": "ignored"}
    v = StoryboardVersion.from_dict(d)
    assert v.version == 1
    assert not hasattr(v, "extra")


def test_storyboard_history_empty():
    h = StoryboardHistory()
    assert h.latest is None
    assert h.versions == []


def test_storyboard_history_add():
    h = StoryboardHistory()
    v1 = h.add("brief 1", "# SB 1", "model", is_initial=True)
    assert v1.version == 1
    assert v1.is_initial is True
    assert len(h.versions) == 1
    assert h.latest is v1


def test_storyboard_history_add_multiple():
    h = StoryboardHistory()
    h.add("brief 1", "# SB 1", "m", is_initial=True)
    v2 = h.add("refine 1", "# SB 2", "m", is_initial=False)
    assert v2.version == 2
    assert v2.is_initial is False
    assert h.latest is v2
    assert len(h.versions) == 2


def test_storyboard_history_update_latest():
    h = StoryboardHistory()
    h.add("brief", "# Original", "m", is_initial=True)
    h.update_latest("# Edited")
    assert h.latest.storyboard == "# Edited"
    assert h.latest.updated != ""


def test_storyboard_history_update_latest_empty():
    h = StoryboardHistory()
    h.update_latest("# Edited")  # should not raise
    assert h.latest is None


def test_storyboard_history_to_dict_roundtrip():
    h = StoryboardHistory()
    h.add("brief", "# SB", "m", is_initial=True)
    h.add("refine", "# SB 2", "m", is_initial=False)
    d = h.to_dict()
    assert len(d["versions"]) == 2
    h2 = StoryboardHistory.from_dict(d)
    assert len(h2.versions) == 2
    assert h2.versions[0].prompt == "brief"
    assert h2.versions[1].prompt == "refine"


# --- Persistence -----------------------------------------------------------

def test_save_and_load_history(tmp_working: Path):
    h = StoryboardHistory()
    h.add("brief", "# SB", "m", is_initial=True)
    save_history(str(tmp_working), h)
    # File should exist
    assert (paths.storyboard_dir(str(tmp_working)) / HISTORY_FILENAME).exists()
    # Load back
    h2 = load_history(str(tmp_working))
    assert len(h2.versions) == 1
    assert h2.versions[0].prompt == "brief"
    assert h2.versions[0].storyboard == "# SB"


def test_load_history_missing_file(tmp_path: Path):
    h = load_history(str(tmp_path))
    assert h.versions == []


def test_load_history_corrupt_file(tmp_working: Path):
    (paths.storyboard_dir(str(tmp_working)) / HISTORY_FILENAME).write_text("not json", encoding="utf-8")
    h = load_history(str(tmp_working))
    assert h.versions == []


def test_save_and_load_latest_storyboard(tmp_working: Path):
    save_latest_storyboard(str(tmp_working), "# Storyboard\n\nContent")
    assert (paths.storyboard_dir(str(tmp_working)) / LATEST_FILENAME).exists()
    loaded = load_latest_storyboard(str(tmp_working))
    assert loaded == "# Storyboard\n\nContent"


def test_load_latest_storyboard_missing(tmp_path: Path):
    assert load_latest_storyboard(str(tmp_path)) == ""


def test_export_storyboard(tmp_working: Path):
    p = export_storyboard(str(tmp_working), "# Exported SB")
    assert p.exists()
    assert p.name == EXPORT_FILENAME
    assert p.read_text(encoding="utf-8") == "# Exported SB"


def test_save_history_creates_dir(tmp_path: Path):
    # No .llama-cut/storyboard/ dir yet
    h = StoryboardHistory()
    h.add("b", "# SB", "m", True)
    save_history(str(tmp_path), h)
    assert (paths.storyboard_dir(str(tmp_path)) / HISTORY_FILENAME).exists()


def test_save_latest_creates_dir(tmp_path: Path):
    save_latest_storyboard(str(tmp_path), "# SB")
    assert (paths.storyboard_dir(str(tmp_path)) / LATEST_FILENAME).exists()


# --- StoryboardSettings ----------------------------------------------------

def test_storyboard_settings_defaults():
    s = StoryboardSettings()
    assert s.last_brief == ""


def test_storyboard_settings_to_dict():
    s = StoryboardSettings(last_brief="test brief")
    d = s.to_dict()
    assert d["last_brief"] == "test brief"


def test_storyboard_settings_from_dict():
    s = StoryboardSettings.from_dict({"last_brief": "hello", "extra": "ignored"})
    assert s.last_brief == "hello"


def test_storyboard_settings_from_dict_empty():
    s = StoryboardSettings.from_dict({})
    assert s.last_brief == ""


# --- clear_storyboard -------------------------------------------------------

def test_clear_storyboard_removes_all_artefacts(tmp_working: Path):
    # Create some artefacts
    h = StoryboardHistory()
    h.add("brief", "# SB", "m", is_initial=True)
    save_history(str(tmp_working), h)
    save_latest_storyboard(str(tmp_working), "# Storyboard")
    export_storyboard(str(tmp_working), "# Exported")
    sb_dir = paths.storyboard_dir(str(tmp_working))
    assert (sb_dir / HISTORY_FILENAME).exists()
    assert (sb_dir / LATEST_FILENAME).exists()
    assert (sb_dir / EXPORT_FILENAME).exists()
    # Clear
    clear_storyboard(str(tmp_working))
    # Everything is gone — the directory itself is removed
    assert not sb_dir.exists()


def test_clear_storyboard_no_dir(tmp_path: Path):
    # Should not raise if the storyboard directory doesn't exist
    clear_storyboard(str(tmp_path))
    assert not paths.storyboard_dir(str(tmp_path)).exists()


def test_clear_storyboard_load_returns_empty_after(tmp_working: Path):
    h = StoryboardHistory()
    h.add("brief", "# SB", "m", is_initial=True)
    save_history(str(tmp_working), h)
    save_latest_storyboard(str(tmp_working), "# Storyboard")
    clear_storyboard(str(tmp_working))
    # After clearing, loading returns empty state
    assert load_history(str(tmp_working)).versions == []
    assert load_latest_storyboard(str(tmp_working)) == ""