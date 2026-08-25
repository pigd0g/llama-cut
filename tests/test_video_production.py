from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.video_production import (
    EDITING_SYSTEM_PROMPT,
    MAX_AGENT_ROUND_TRIPS,
    EDIT_PLAN_FILENAME,
    TOOL_LOG_FILENAME,
    CHAT_FILENAME,
    SUPPORTED_TRANSITIONS,
    SUPPORTED_PRESETS,
    STAGE_WEIGHTS,
    VideoProductionConfig,
    VideoProductionSettings,
    EditPlan,
    EditFormat,
    EditCommand,
    TimelineItem,
    TransitionSpec,
    AudioPlan,
    ToolResult,
    ToolRegistry,
    load_video_production_config,
    is_config_valid,
    build_ollama_client,
    save_edit_plan,
    load_edit_plan,
    save_tool_log,
    load_tool_log,
    load_tool_log_meta,
    save_chat,
    load_chat,
    clear_production,
    extract_beat_thumbnail,
    load_ffmpeg_skill,
    _sanitize_name,
    _is_safe_output_path,
)
from src import paths


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture
def tmp_working(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def registry(tmp_working: Path) -> ToolRegistry:
    """Build a ToolRegistry with 2 fake source videos."""
    v1 = SimpleNamespace(name="ClipA.mp4", path=str(tmp_working / "ClipA.mp4"))
    v2 = SimpleNamespace(name="ClipB.mp4", path=str(tmp_working / "ClipB.mp4"))
    from src.video_metadata import VideoMetadata
    m1 = VideoMetadata(source_filename="ClipA.mp4", source_path=v1.path,
                       duration=60.0, duration_hms="00:01:00",
                       width=1920, height=1080, frame_rate=25.0)
    m2 = VideoMetadata(source_filename="ClipB.mp4", source_path=v2.path,
                       duration=30.0, duration_hms="00:00:30",
                       width=1920, height=1080, frame_rate=25.0)
    return ToolRegistry(str(tmp_working), [v1, v2], [m1, m2])


# --- Config ------------------------------------------------------------------

def test_load_config_reads_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    monkeypatch.setenv("OLLAMA_WORKFLOW_MODEL", "gemma4:31b")
    cfg = load_video_production_config()
    assert cfg == VideoProductionConfig("http://localhost:11434", "secret", "gemma4:31b")


def test_load_config_strips_whitespace(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "  http://localhost  ")
    monkeypatch.setenv("OLLAMA_API_KEY", "  key  ")
    monkeypatch.setenv("OLLAMA_WORKFLOW_MODEL", "  gemma4  ")
    cfg = load_video_production_config()
    assert cfg.host == "http://localhost"
    assert cfg.api_key == "key"
    assert cfg.model == "gemma4"


def test_load_config_missing_vars(monkeypatch):
    for v in ("OLLAMA_HOST", "OLLAMA_API_KEY", "OLLAMA_WORKFLOW_MODEL"):
        monkeypatch.delenv(v, raising=False)
    cfg = load_video_production_config()
    assert cfg.host == ""
    assert cfg.api_key == ""
    assert cfg.model == ""


def test_is_config_valid_ok():
    cfg = VideoProductionConfig("http://localhost:11434", "", "gemma4")
    ok, msg = is_config_valid(cfg)
    assert ok is True
    assert msg == ""


def test_is_config_valid_ok_with_api_key():
    cfg = VideoProductionConfig("https://ollama.com", "key", "gemma4")
    ok, _ = is_config_valid(cfg)
    assert ok is True


def test_is_config_valid_missing_host():
    cfg = VideoProductionConfig("", "key", "gemma4")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_HOST" in msg


def test_is_config_valid_missing_model():
    cfg = VideoProductionConfig("http://localhost", "", "")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_WORKFLOW_MODEL" in msg


# --- build_ollama_client -----------------------------------------------------

def test_build_ollama_client_with_api_key():
    cfg = VideoProductionConfig("http://localhost:11434", "secret", "gemma4")
    with patch("ollama.Client") as mock_cls:
        build_ollama_client(cfg)
        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        assert kwargs["host"] == "http://localhost:11434"
        assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_build_ollama_client_without_api_key():
    cfg = VideoProductionConfig("http://localhost:11434", "", "gemma4")
    with patch("ollama.Client") as mock_cls:
        build_ollama_client(cfg)
        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        assert kwargs["headers"] is None


# --- Pydantic models ---------------------------------------------------------

def test_timeline_item_defaults():
    item = TimelineItem(id="shot_01", source="ClipA.mp4",
                        source_start=0.0, source_end=5.0)
    assert item.speed == 1.0
    assert item.transition_out is None
    assert item.transition_duration == 0.0
    assert item.storyboard_shot == ""
    assert item.intermediate_clip == ""
    assert item.status == "draft"
    assert item.thumbnail_path == ""


def test_timeline_item_speed_must_be_positive():
    with pytest.raises(Exception):
        TimelineItem(id="s1", source="a.mp4", source_start=0, source_end=5, speed=0)


def test_transition_spec_defaults():
    t = TransitionSpec(after="shot_01", type="dissolve")
    assert t.duration == 0.0


def test_audio_plan_defaults():
    a = AudioPlan()
    assert a.volume == 1.0
    assert a.fade_in == 0.0
    assert a.fade_out == 0.0
    assert a.normalize is False


def test_edit_format_defaults():
    f = EditFormat()
    assert f.width == 1920
    assert f.height == 1080
    assert f.fps == 30.0


def test_edit_plan_defaults():
    plan = EditPlan()
    assert plan.version == 1
    assert plan.timeline == []
    assert plan.commands == []
    assert plan.transitions == []
    assert plan.storyboard_version == 0


def test_edit_command_defaults():
    cmd = EditCommand(id="cmd01", type="extract_clip", args={})
    assert cmd.id == "cmd01"
    assert cmd.type == "extract_clip"
    assert cmd.beat_id is None
    assert cmd.args == {}
    assert cmd.status == "pending"


def test_edit_plan_serialization_roundtrip():
    plan = EditPlan(
        version=2,
        target_duration=120.0,
        format=EditFormat(width=3840, height=2160, fps=60.0),
        timeline=[
            TimelineItem(id="shot_01", source="A.mp4",
                         source_start=10.0, source_end=15.0,
                         storyboard_shot="Scene 1"),
        ],
        commands=[
            EditCommand(id="cmd01", type="extract_clip", beat_id="shot_01",
                        args={"source": "A.mp4", "start_time": 10.0,
                              "end_time": 15.0, "output_name": "shot_01"}),
        ],
        transitions=[TransitionSpec(after="shot_01", type="dissolve", duration=1.0)],
        audio=AudioPlan(volume=1.5, fade_in=2.0, normalize=True),
        storyboard_version=3,
        storyboard_sha="abc123",
    )
    json_str = plan.model_dump_json()
    restored = EditPlan.model_validate_json(json_str)
    assert restored.version == 2
    assert restored.target_duration == 120.0
    assert restored.format.width == 3840
    assert len(restored.timeline) == 1
    assert restored.timeline[0].storyboard_shot == "Scene 1"
    assert len(restored.commands) == 1
    assert restored.commands[0].type == "extract_clip"
    assert restored.commands[0].beat_id == "shot_01"
    assert restored.transitions[0].type == "dissolve"
    assert restored.audio.normalize is True


# --- Helpers -----------------------------------------------------------------

def test_sanitize_name_basic():
    assert _sanitize_name("clip_01") == "clip_01"
    assert _sanitize_name("my clip/name") == "my_clip_name"


def test_sanitize_name_strips_slashes():
    assert _sanitize_name("../evil") == ".._evil"
    assert _sanitize_name("clip/name") == "clip_name"


def test_sanitize_name_long_string():
    long = "a" * 200
    assert len(_sanitize_name(long)) == 120


def test_is_safe_output_path_inside(tmp_path):
    base = tmp_path / "clips"
    base.mkdir()
    p = base / "clip.mp4"
    assert _is_safe_output_path(p, base) is True


def test_is_safe_output_path_outside(tmp_path):
    base = tmp_path / "clips"
    base.mkdir()
    other = tmp_path / "evil.mp4"
    assert _is_safe_output_path(other, base) is False


# --- ToolRegistry: probe_video ----------------------------------------------

def test_probe_video_unknown_video(registry):
    result = registry.probe_video("Nonexistent.mp4")
    data = json.loads(result["content"])
    assert data["error"] is not None or "error" in data


def test_probe_video_known_video(registry, tmp_working):
    from src.video_metadata import VideoMetadata
    fake_meta = VideoMetadata(
        source_filename="ClipA.mp4",
        source_path=str(tmp_working / "ClipA.mp4"),
        duration=60.0,
        duration_hms="00:01:00",
        width=1920, height=1080,
        frame_rate=25.0,
        video_codec="h264",
    )
    with patch("src.video_metadata.extract_metadata", return_value=fake_meta):
        result = registry.probe_video("ClipA.mp4")
    data = json.loads(result["content"])
    assert "filename" in data
    assert data["filename"] == "ClipA.mp4"


def test_probe_video_ffprobe_fails(registry):
    with patch("src.video_metadata.extract_metadata", return_value=None):
        result = registry.probe_video("ClipA.mp4")
    data = json.loads(result["content"])
    assert "error" in data


# --- ToolRegistry: inspect_clip ---------------------------------------------

def test_inspect_clip_unknown_video(registry):
    result = registry.inspect_clip("Nonexistent.mp4", 0, 5)
    data = json.loads(result["content"])
    assert "error" in data


def test_inspect_clip_invalid_range(registry):
    result = registry.inspect_clip("ClipA.mp4", 50, 100)
    data = json.loads(result["content"])
    assert "error" in data


# --- ToolRegistry: commit_edit_plan -----------------------------------------

def test_commit_edit_plan_success(registry):
    plan = {
        "timeline": [
            {"id": "shot01", "source": "ClipA.mp4",
             "source_start": 0, "source_end": 5, "purpose": "Hook"},
        ],
        "commands": [
            {"id": "cmd01", "type": "extract_clip", "beat_id": "shot01",
             "args": {"source": "ClipA.mp4", "start_time": 0,
                      "end_time": 5, "output_name": "shot01"}},
        ],
    }
    result = registry.commit_edit_plan(plan)
    data = json.loads(result["content"])
    assert data["timeline_count"] == 1
    assert data["commands_count"] == 1
    assert registry.current_plan is not None
    assert len(registry.current_plan.timeline) == 1


def test_commit_edit_plan_validation_error(registry):
    result = registry.commit_edit_plan({"timeline": "not a list"})
    data = json.loads(result["content"])
    assert "error" in data


def test_commit_edit_plan_warnings_for_unknown_source(registry):
    plan = {
        "timeline": [
            {"id": "shot01", "source": "Nonexistent.mp4",
             "source_start": 0, "source_end": 5},
        ],
        "commands": [],
    }
    result = registry.commit_edit_plan(plan)
    data = json.loads(result["content"])
    assert "warnings" in data
    assert any("unknown source" in w.lower() for w in data["warnings"])


def test_update_edit_plan_replaces(registry):
    plan1 = {
        "timeline": [{"id": "shot01", "source": "ClipA.mp4",
                      "source_start": 0, "source_end": 5}],
        "commands": [],
    }
    registry.commit_edit_plan(plan1)
    assert len(registry.current_plan.timeline) == 1

    plan2 = {
        "timeline": [
            {"id": "shot01", "source": "ClipA.mp4",
             "source_start": 0, "source_end": 5},
            {"id": "shot02", "source": "ClipB.mp4",
             "source_start": 0, "source_end": 3},
        ],
        "commands": [],
    }
    registry.update_edit_plan(plan2)
    assert len(registry.current_plan.timeline) == 2


# --- ToolRegistry: only 4 tools ---------------------------------------------

def test_registry_has_four_tools(registry):
    tools = registry.get_tools()
    names = [getattr(t, "__name__", "") for t in tools]
    assert "probe_video" in names
    assert "inspect_clip" in names
    assert "commit_edit_plan" in names
    assert "update_edit_plan" in names
    assert len(tools) == 4


def test_registry_no_execution_tools(registry):
    """The 8 execution tools (extract_clip, render_video, etc.) must be gone."""
    tools = registry.get_tools()
    names = [getattr(t, "__name__", "") for t in tools]
    assert "extract_clip" not in names
    assert "create_transition" not in names
    assert "render_video" not in names
    assert "assemble_timeline" not in names
    assert "validate_clip" not in names
    assert "validate_video" not in names
    assert "mix_audio" not in names
    assert "create_edit" not in names


# --- Persistence -------------------------------------------------------------

def test_save_and_load_edit_plan(tmp_working):
    plan = EditPlan(
        version=1, target_duration=30.0,
        timeline=[TimelineItem(id="shot_01", source="A.mp4",
                                source_start=0, source_end=5)],
        commands=[EditCommand(id="cmd01", type="extract_clip",
                              beat_id="shot_01",
                              args={"source": "A.mp4", "start_time": 0,
                                    "end_time": 5, "output_name": "shot_01"})],
    )
    save_edit_plan(str(tmp_working), plan)
    loaded = load_edit_plan(str(tmp_working))
    assert loaded is not None
    assert loaded.version == 1
    assert loaded.target_duration == 30.0
    assert len(loaded.timeline) == 1
    assert loaded.timeline[0].id == "shot_01"
    assert len(loaded.commands) == 1
    assert loaded.commands[0].type == "extract_clip"


def test_load_edit_plan_missing(tmp_working):
    assert load_edit_plan(str(tmp_working)) is None


def test_save_and_load_tool_log(tmp_working):
    log = [
        {"tool": "probe_video", "args": {"video_path": "A.mp4"},
         "result": {"duration": 60.0}, "success": True, "duration_s": 0.5},
    ]
    save_tool_log(str(tmp_working), log)
    loaded = load_tool_log(str(tmp_working))
    assert len(loaded) == 1
    assert loaded[0]["tool"] == "probe_video"


def test_load_tool_log_missing(tmp_working):
    assert load_tool_log(str(tmp_working)) == []


def test_tool_log_roundtrip_new_format(tmp_working):
    """The current format is {meta, entries}; entries load, meta loads."""
    log = {
        "meta": {
            "plan_status": "failed",
            "total_commands": 3,
            "ran": 2,
            "succeeded": 1,
            "skipped": 0,
            "failed": 1,
            "not_run": 1,
            "timestamp": "2026-08-25T12:00:00",
        },
        "entries": [
            {"id": "c1", "type": "extract_clip", "status": "done",
             "command": "ffmpeg ...", "output_path": "x.mp4",
             "error": "", "stderr": "", "duration_s": 1.0},
            {"id": "c2", "type": "render_video", "status": "failed",
             "command": "ffmpeg ...", "output_path": "",
             "error": "boom", "stderr": "error line", "duration_s": 2.0},
            {"id": "c3", "type": "validate", "status": "not_run",
             "command": "ffprobe ...", "output_path": "",
             "error": "", "stderr": "", "duration_s": 0.0},
        ],
    }
    save_tool_log(str(tmp_working), log)
    loaded = load_tool_log(str(tmp_working))
    assert len(loaded) == 3
    assert [e["status"] for e in loaded] == ["done", "failed", "not_run"]
    meta = load_tool_log_meta(str(tmp_working))
    assert meta["plan_status"] == "failed"
    assert meta["not_run"] == 1


def test_tool_log_legacy_list_still_loads(tmp_working):
    """Old plain-list logs keep working (backward compat)."""
    save_tool_log(str(tmp_working), [
        {"id": "c1", "type": "extract_clip", "status": "done",
         "command": "ffmpeg ...", "output_path": "x.mp4",
         "error": "", "stderr": "", "duration_s": 1.0},
    ])
    assert len(load_tool_log(str(tmp_working))) == 1
    assert load_tool_log_meta(str(tmp_working)) == {}


def test_load_tool_log_meta_missing(tmp_working):
    assert load_tool_log_meta(str(tmp_working)) == {}


def test_persist_exec_log_records_all_commands(tmp_working):
    """The execution log covers every plan command: done, skipped, failed, not_run."""
    from PyQt6.QtCore import QCoreApplication
    QCoreApplication.instance() or QCoreApplication([])
    from src.workers.edit_executor_worker import EditExecutorWorker
    from src.edit_plan_executor import CommandResult

    plan = EditPlan(commands=[
        EditCommand(id="c1", type="extract_clip", args={"output_name": "a"}),
        EditCommand(id="c2", type="extract_clip", args={"output_name": "b"}),
        EditCommand(id="c3", type="render_video", args={"output_name": "final.mp4"}),
    ])
    worker = EditExecutorWorker(str(tmp_working), plan)
    worker._executor = MagicMock()
    results = [
        CommandResult(plan.commands[0], True, output_path="a.mp4", duration_s=1.0),
        CommandResult(plan.commands[1], False, error="boom", stderr="err", duration_s=2.0),
        # c3 never ran (run halted on c2's failure)
    ]
    worker._persist_exec_log(results)

    entries = load_tool_log(str(tmp_working))
    assert [e["status"] for e in entries] == ["done", "failed", "not_run"]
    assert entries[2]["id"] == "c3"
    assert entries[2]["command"]  # rendered command line present even for not_run
    meta = load_tool_log_meta(str(tmp_working))
    assert meta["total_commands"] == 3
    assert meta["ran"] == 2
    assert meta["succeeded"] == 1
    assert meta["failed"] == 1
    assert meta["not_run"] == 1
    assert meta["plan_status"] == "draft"


def test_save_and_load_chat(tmp_working):
    messages = [
        {"role": "user", "content": "Make it short"},
        {"role": "assistant", "content": "I'll trim it."},
    ]
    save_chat(str(tmp_working), messages)
    loaded = load_chat(str(tmp_working))
    assert len(loaded) == 2
    assert loaded[0]["role"] == "user"
    assert loaded[1]["content"] == "I'll trim it."


def test_load_chat_missing(tmp_working):
    assert load_chat(str(tmp_working)) == []


def test_clear_production(tmp_working):
    d = paths.video_dir(str(tmp_working))
    d.mkdir(parents=True)
    (d / "edit_plan.json").write_text("{}")
    clear_production(str(tmp_working))
    assert not d.exists()


def test_clear_production_nonexistent(tmp_working):
    clear_production(str(tmp_working))  # should not raise


# --- VideoProductionSettings -------------------------------------------------

def test_video_production_settings_defaults():
    s = VideoProductionSettings()
    assert s.last_feedback == ""


def test_video_production_settings_to_dict():
    s = VideoProductionSettings(last_feedback="shorter")
    d = s.to_dict()
    assert d == {"last_feedback": "shorter"}


def test_video_production_settings_from_dict():
    s = VideoProductionSettings.from_dict({"last_feedback": "test", "extra": "ignore"})
    assert s.last_feedback == "test"


# --- Constants ---------------------------------------------------------------

def test_max_agent_round_trips_is_reasonable():
    assert MAX_AGENT_ROUND_TRIPS == 80


def test_supported_transitions_includes_common():
    assert "cut" in SUPPORTED_TRANSITIONS
    assert "dissolve" in SUPPORTED_TRANSITIONS
    assert "fadeblack" in SUPPORTED_TRANSITIONS


def test_supported_presets_includes_common():
    assert "preview" in SUPPORTED_PRESETS
    assert "youtube_1080p" in SUPPORTED_PRESETS
    assert "youtube_4k" in SUPPORTED_PRESETS
    assert "high_quality" in SUPPORTED_PRESETS


def test_stage_weights_sum_to_one():
    total = sum(STAGE_WEIGHTS.values())
    assert abs(total - 1.0) < 0.001


def test_stage_weights_cover_all_stages():
    expected = {"extract", "transitions", "assemble", "audio", "render", "validate"}
    assert set(STAGE_WEIGHTS.keys()) == expected


# --- ToolResult --------------------------------------------------------------

def test_tool_result_to_tool_message():
    r = ToolResult(True, {"duration": 60.0})
    msg = r.to_tool_message()
    assert msg["role"] == "tool"
    assert json.loads(msg["content"]) == {"duration": 60.0}


# --- System prompt -----------------------------------------------------------

def test_editing_system_prompt_mentions_plan():
    assert "edit plan" in EDITING_SYSTEM_PROMPT.lower()
    assert "commit_edit_plan" in EDITING_SYSTEM_PROMPT


def test_editing_system_prompt_no_execution_tools():
    """The prompt must not instruct the agent to run ffmpeg execution tools."""
    # The system prompt should not reference the removed execution tools
    # as tools the agent calls (only as command types in the plan).
    # It's OK for them to appear as command type descriptions.
    assert "You do **NOT** run FFmpeg" in EDITING_SYSTEM_PROMPT


# --- FFmpeg skill loading ----------------------------------------------------

def test_load_ffmpeg_skill_returns_content():
    skill = load_ffmpeg_skill()
    assert len(skill) > 0
    assert "ffmpeg" in skill.lower()


# --- Beat thumbnail ----------------------------------------------------------

def test_extract_beat_thumbnail_caches(tmp_working):
    # Create a fake source video
    src = tmp_working / "ClipA.mp4"
    src.write_bytes(b"fake")

    def mock_run(cmd, timeout=1800):
        # Simulate ffmpeg writing a jpg
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake jpg")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        thumb1 = extract_beat_thumbnail(
            str(tmp_working), str(src), 10.0, 20.0, "shot01",
        )
    assert thumb1 != ""
    assert Path(thumb1).exists()

    # Second call should use the cache (no ffmpeg call)
    with patch("src.video_production._run_ffmpeg") as mock_run2:
        thumb2 = extract_beat_thumbnail(
            str(tmp_working), str(src), 10.0, 20.0, "shot01",
        )
    assert thumb2 == thumb1
    mock_run2.assert_not_called()