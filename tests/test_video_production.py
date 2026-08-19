from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.video_production import (
    EDITING_SYSTEM_PROMPT,
    REFINEMENT_INSTRUCTIONS,
    MAX_AGENT_ROUND_TRIPS,
    VIDEO_DIR,
    EDIT_PLAN_FILENAME,
    TOOL_LOG_FILENAME,
    SUPPORTED_TRANSITIONS,
    SUPPORTED_PRESETS,
    VideoProductionConfig,
    VideoProductionSettings,
    EditPlan,
    EditFormat,
    TimelineItem,
    TransitionSpec,
    AudioPlan,
    ToolResult,
    ToolRegistry,
    load_video_production_config,
    is_config_valid,
    build_ollama_client,
    build_generation_prompt,
    build_refinement_prompt,
    run_editing_agent,
    save_edit_plan,
    load_edit_plan,
    save_tool_log,
    load_tool_log,
    clear_production,
    build_edit_plan_from_tool_log,
    _sanitize_name,
    _is_safe_output_path,
)


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


def test_load_config_uses_workflow_model_not_vision(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost")
    monkeypatch.setenv("OLLAMA_API_KEY", "")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "vision_model")
    monkeypatch.setenv("OLLAMA_WORKFLOW_MODEL", "workflow_model")
    cfg = load_video_production_config()
    assert cfg.model == "workflow_model"


# --- is_config_valid ----------------------------------------------------------

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


def test_is_config_valid_missing_both():
    cfg = VideoProductionConfig("", "", "")
    ok, msg = is_config_valid(cfg)
    assert ok is False
    assert "OLLAMA_HOST" in msg
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
    assert item.transition_in is None
    assert item.transition_out is None
    assert item.storyboard_shot == ""
    assert item.intermediate_clip == ""


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
    assert plan.transitions == []
    assert plan.storyboard_version == 0


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


# --- ToolRegistry: extract_clip ----------------------------------------------

def test_extract_clip_unknown_video(registry):
    result = registry.extract_clip("Nonexistent.mp4", 0, 5, "clip1")
    data = json.loads(result["content"])
    assert "error" in data


def test_extract_clip_time_exceeds_duration(registry):
    result = registry.extract_clip("ClipA.mp4", 50, 100, "clip1")
    data = json.loads(result["content"])
    assert "error" in data


def test_extract_clip_start_after_end(registry):
    result = registry.extract_clip("ClipA.mp4", 10, 5, "clip1")
    data = json.loads(result["content"])
    assert "error" in data


def test_extract_clip_success_reencode(registry, tmp_working):
    # Create a fake source video file
    src = tmp_working / "ClipA.mp4"
    src.write_bytes(b"fake")

    def mock_run(cmd, timeout=1800):
        # Verify it re-encodes (not stream copy)
        assert "libx264" in cmd, "extract_clip should re-encode to bake rotation"
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"fake clip")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        result = registry.extract_clip("ClipA.mp4", 0, 5, "clip1")
    data = json.loads(result["content"])
    assert data.get("output_name") == "clip1"
    assert "output_path" in data


# --- ToolRegistry: create_transition ----------------------------------------

def test_create_transition_unsupported_type(registry, tmp_working):
    result = registry.create_transition("a", "b", "crazy_effect", 1.0, "out")
    data = json.loads(result["content"])
    assert "error" in data
    assert "Unsupported transition" in data["error"]


def test_create_transition_clip_not_found(registry):
    result = registry.create_transition("nonexistent", "also_nonexistent",
                                          "dissolve", 1.0, "out")
    data = json.loads(result["content"])
    assert "error" in data


def test_create_transition_cut_success(registry, tmp_working):
    clips_dir = registry._clips_dir
    (clips_dir / "a.mp4").write_bytes(b"fake")
    (clips_dir / "b.mp4").write_bytes(b"fake")
    registry._intermediate_clips["a"] = str(clips_dir / "a.mp4")
    registry._intermediate_clips["b"] = str(clips_dir / "b.mp4")

    def mock_run(cmd, timeout=1800):
        Path(cmd[-1]).write_bytes(b"fake")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        result = registry.create_transition("a", "b", "cut", 0, "out")
    data = json.loads(result["content"])
    assert data.get("transition") == "cut"


# --- ToolRegistry: render_video ---------------------------------------------

def test_render_video_unsupported_preset(registry, tmp_working):
    result = registry.render_video("timeline", "final.mp4", preset="bad_preset")
    data = json.loads(result["content"])
    assert "error" in data
    assert "Unsupported preset" in data["error"]


def test_render_video_timeline_not_found(registry):
    result = registry.render_video("nonexistent", "final.mp4", preset="preview")
    data = json.loads(result["content"])
    assert "error" in data


def test_render_video_success(registry, tmp_working):
    clips_dir = registry._clips_dir
    timeline_path = clips_dir / "timeline.mp4"
    timeline_path.write_bytes(b"fake")
    registry._intermediate_clips["timeline"] = str(timeline_path)

    def mock_run(cmd, timeout=3600):
        Path(cmd[-1]).write_bytes(b"fake render")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        with patch("src.video_production.is_nvenc_available", return_value=False):
            result = registry.render_video("timeline", "final.mp4", preset="preview")
    data = json.loads(result["content"])
    assert "output_path" in data
    assert data["preset"] == "preview"
    assert data["codec"] == "libx264"


def test_render_video_nvenc_success(registry, tmp_working):
    clips_dir = registry._clips_dir
    timeline_path = clips_dir / "timeline.mp4"
    timeline_path.write_bytes(b"fake")
    registry._intermediate_clips["timeline"] = str(timeline_path)

    def mock_run(cmd, timeout=3600):
        assert "h264_nvenc" in cmd, "should use NVENC when available"
        Path(cmd[-1]).write_bytes(b"fake render")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        with patch("src.video_production.is_nvenc_available", return_value=True):
            result = registry.render_video("timeline", "final.mp4", preset="preview")
    data = json.loads(result["content"])
    assert "output_path" in data
    assert data["codec"] == "h264_nvenc"


def test_render_video_h265_nvenc(registry, tmp_working):
    clips_dir = registry._clips_dir
    timeline_path = clips_dir / "timeline.mp4"
    timeline_path.write_bytes(b"fake")
    registry._intermediate_clips["timeline"] = str(timeline_path)

    def mock_run(cmd, timeout=3600):
        assert "hevc_nvenc" in cmd, "should use hevc_nvenc for h265"
        Path(cmd[-1]).write_bytes(b"fake render")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        with patch("src.video_production.is_nvenc_available", return_value=True):
            result = registry.render_video("timeline", "final.mp4",
                                           preset="preview", video_codec="h265")
    data = json.loads(result["content"])
    assert data["codec"] == "hevc_nvenc"


def test_render_video_nvenc_fallback_to_software(registry, tmp_working):
    clips_dir = registry._clips_dir
    timeline_path = clips_dir / "timeline.mp4"
    timeline_path.write_bytes(b"fake")
    registry._intermediate_clips["timeline"] = str(timeline_path)

    call_count = [0]
    def mock_run(cmd, timeout=3600):
        call_count[0] += 1
        if call_count[0] == 1:
            # NVENC fails
            return 1, "", "NVENC not available"
        # Software encoder succeeds
        assert "libx264" in cmd, "should fall back to libx264"
        Path(cmd[-1]).write_bytes(b"fake render")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        with patch("src.video_production.is_nvenc_available", return_value=True):
            result = registry.render_video("timeline", "final.mp4", preset="preview")
    data = json.loads(result["content"])
    assert "output_path" in data
    assert data["codec"] == "libx264"
    assert call_count[0] == 2


# --- ToolRegistry: validate_video --------------------------------------------

def test_validate_video_file_not_found(registry):
    result = registry.validate_video("nonexistent.mp4")
    data = json.loads(result["content"])
    assert "error" in data


def test_validate_video_success(registry, tmp_working):
    out_dir = registry._output_dir
    (out_dir / "final.mp4").write_bytes(b"fake")

    from src.ffmpeg.probe import ProbeResult
    fake_probe = ProbeResult(
        duration=60.0, width=1920, height=1080, codec="h264",
        fps=25.0, raw={},
    )
    with patch("src.ffmpeg.probe.run_ffprobe", return_value=fake_probe):
        result = registry.validate_video("final.mp4")
    data = json.loads(result["content"])
    assert data["passed"] is True
    assert data["duration"] == 60.0


def test_validate_video_with_expected_mismatch(registry, tmp_working):
    out_dir = registry._output_dir
    (out_dir / "final.mp4").write_bytes(b"fake")

    from src.ffmpeg.probe import ProbeResult
    fake_probe = ProbeResult(
        duration=30.0, width=1280, height=720, codec="h264",
        fps=25.0, raw={},
    )
    with patch("src.ffmpeg.probe.run_ffprobe", return_value=fake_probe):
        result = registry.validate_video("final.mp4",
                                          expected_duration=60.0,
                                          expected_resolution="1920x1080")
    data = json.loads(result["content"])
    assert data["passed"] is False
    assert len(data["issues"]) == 2


# --- ToolRegistry: assemble_timeline -----------------------------------------

def test_assemble_timeline_no_clips(registry):
    result = registry.assemble_timeline([], output_name="timeline")
    data = json.loads(result["content"])
    assert "error" in data


def test_assemble_timeline_missing_clips(registry):
    result = registry.assemble_timeline(["a", "b"], output_name="timeline")
    data = json.loads(result["content"])
    assert "error" in data


def test_assemble_timeline_success_concat(registry, tmp_working):
    clips_dir = registry._clips_dir
    for name in ("a.mp4", "b.mp4"):
        (clips_dir / name).write_bytes(b"fake")
    registry._intermediate_clips["a"] = str(clips_dir / "a.mp4")
    registry._intermediate_clips["b"] = str(clips_dir / "b.mp4")

    def mock_run(cmd, timeout=1800):
        # Verify it re-encodes (not stream copy) to bake rotation
        assert "libx264" in cmd, "assemble_timeline should re-encode concat"
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"fake timeline")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        result = registry.assemble_timeline(["a", "b"], output_name="timeline")
    data = json.loads(result["content"])
    assert data.get("clips_assembled") == 2


def test_assemble_timeline_with_transitions(registry, tmp_working):
    clips_dir = registry._clips_dir
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (clips_dir / name).write_bytes(b"fake")
    registry._intermediate_clips["a"] = str(clips_dir / "a.mp4")
    registry._intermediate_clips["b"] = str(clips_dir / "b.mp4")
    registry._intermediate_clips["c"] = str(clips_dir / "c.mp4")

    def mock_run(cmd, timeout=1800):
        # Verify the filter_complex uses labeled streams (not raw [0:v])
        fc_idx = cmd.index("-filter_complex") if "-filter_complex" in cmd else -1
        if fc_idx >= 0:
            fc = cmd[fc_idx + 1]
            # Should use normalized labels like [nv0], [na0]
            assert "[nv0]" in fc, "should use normalized video labels"
            assert "[na0]" in fc, "should use normalized audio labels"
            # Should use xfade for dissolve, not concat
            assert "xfade" in fc, "should use xfade for dissolve transitions"
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"fake timeline")
        return 0, "", ""

    with patch("src.video_production._run_ffmpeg", side_effect=mock_run):
        with patch.object(registry, "_probe_duration", return_value=10.0):
            result = registry.assemble_timeline(
                ["a", "b", "c"],
                transitions=[
                    {"after": "a", "type": "dissolve", "duration": 1.0},
                    {"after": "b", "type": "cut", "duration": 0.0},
                ],
                output_name="timeline_trans",
            )
    data = json.loads(result["content"])
    assert data.get("clips_assembled") == 3
    assert data.get("transitions") == 2


# --- Agent loop --------------------------------------------------------------

def test_agent_loop_no_tool_calls():
    """Agent responds with text only — loop runs once."""
    mock_client = MagicMock()
    mock_msg = MagicMock()
    mock_msg.tool_calls = None
    mock_msg.content = "Done editing."
    mock_client.chat.return_value = MagicMock(message=mock_msg)

    final_text, tool_log = run_editing_agent(
        mock_client, "model", "system", "user", tools=[],
    )
    assert final_text == "Done editing."
    assert tool_log == []


def test_agent_loop_one_tool_then_done():
    """Agent calls one tool, then responds with text."""
    mock_client = MagicMock()
    call_count = [0]
    def chat_side_effect(**kwargs):
        call_count[0] += 1
        msg = MagicMock()
        if call_count[0] == 1:
            mock_call = MagicMock()
            mock_call.function.name = "probe_video"
            mock_call.function.arguments = {"video_path": "ClipA.mp4"}
            msg.tool_calls = [mock_call]
            msg.content = ""
        else:
            msg.tool_calls = None
            msg.content = "Finished."
        return MagicMock(message=msg)

    mock_client.chat.side_effect = chat_side_effect

    def fake_tool(video_path: str) -> dict:
        return {"filename": video_path}

    final_text, tool_log = run_editing_agent(
        mock_client, "model", "system", "user",
        tools=[fake_tool],
    )
    assert final_text == "Finished."
    assert len(tool_log) == 1
    assert tool_log[0]["tool"] == "probe_video"


def test_agent_loop_exhaustion():
    """Agent keeps calling tools until the limit is reached."""
    mock_client = MagicMock()
    msg = MagicMock()
    msg.tool_calls = [MagicMock()]
    msg.tool_calls[0].function.name = "probe_video"
    msg.tool_calls[0].function.arguments = {}
    msg.content = ""
    mock_client.chat.return_value = MagicMock(message=msg)

    final_text, tool_log = run_editing_agent(
        mock_client, "model", "system", "user",
        tools=[lambda **kw: {"ok": True}],
        log_cb=lambda s: None,
    )
    assert "exhausted" in final_text.lower() or "did not complete" in final_text.lower()
    assert len(tool_log) == MAX_AGENT_ROUND_TRIPS


# --- Prompt building --------------------------------------------------------

def test_build_generation_prompt_includes_storyboard():
    prompt = build_generation_prompt("# Story\n\nScene 1", "# Context\n\nInfo")
    assert "Story" in prompt
    assert "Context" in prompt
    assert "Task" in prompt


def test_build_refinement_prompt_includes_feedback():
    prompt = build_refinement_prompt(
        "Make it shorter", '{"version": 1}', "# Story", "# Context",
    )
    assert "Make it shorter" in prompt
    assert "version" in prompt


# --- Persistence -------------------------------------------------------------

def test_save_and_load_edit_plan(tmp_working):
    plan = EditPlan(
        version=1, target_duration=30.0,
        timeline=[TimelineItem(id="shot_01", source="A.mp4",
                                source_start=0, source_end=5)],
    )
    save_edit_plan(str(tmp_working), plan)
    loaded = load_edit_plan(str(tmp_working))
    assert loaded is not None
    assert loaded.version == 1
    assert loaded.target_duration == 30.0
    assert len(loaded.timeline) == 1
    assert loaded.timeline[0].id == "shot_01"


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


def test_clear_production(tmp_working):
    d = tmp_working / VIDEO_DIR
    d.mkdir()
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


# --- build_edit_plan_from_tool_log -------------------------------------------

def test_build_edit_plan_from_tool_log():
    log = [
        {"tool": "probe_video", "args": {"video_path": "A.mp4"},
         "result": {"duration": 60.0}, "success": True},
        {"tool": "extract_clip",
         "args": {"video_path": "A.mp4", "start_time": 0, "end_time": 5, "output_name": "clip1"},
         "result": {"output_name": "clip1", "output_path": "/clips/clip1.mp4"},
         "success": True},
        {"tool": "extract_clip",
         "args": {"video_path": "A.mp4", "start_time": 10, "end_time": 15, "output_name": "clip2"},
         "result": {"output_name": "clip2", "output_path": "/clips/clip2.mp4"},
         "success": True},
        {"tool": "create_transition",
         "args": {"clip_a": "clip1", "clip_b": "clip2", "transition": "dissolve", "duration": 1.0},
         "result": {"output_name": "trans1"},
         "success": True},
    ]
    plan = build_edit_plan_from_tool_log(log, storyboard_version=2)
    assert plan.storyboard_version == 2
    assert len(plan.timeline) == 2
    assert plan.timeline[0].id == "shot_01"
    assert plan.timeline[0].source == "A.mp4"
    assert plan.timeline[0].source_start == 0
    assert plan.timeline[1].source_start == 10
    assert len(plan.transitions) == 1
    assert plan.transitions[0].type == "dissolve"


def test_build_edit_plan_from_tool_log_skips_errors():
    log = [
        {"tool": "extract_clip",
         "args": {"video_path": "A.mp4", "start_time": 0, "end_time": 5, "output_name": "clip1"},
         "result": {"error": "failed"}, "success": False},
    ]
    plan = build_edit_plan_from_tool_log(log)
    assert len(plan.timeline) == 0


# --- Constants ---------------------------------------------------------------

def test_max_agent_round_trips_is_reasonable():
    assert MAX_AGENT_ROUND_TRIPS == 50


def test_supported_transitions_includes_common():
    assert "cut" in SUPPORTED_TRANSITIONS
    assert "dissolve" in SUPPORTED_TRANSITIONS
    assert "fadeblack" in SUPPORTED_TRANSITIONS


def test_supported_presets_includes_common():
    assert "preview" in SUPPORTED_PRESETS
    assert "youtube_1080p" in SUPPORTED_PRESETS
    assert "high_quality" in SUPPORTED_PRESETS


# --- ToolResult --------------------------------------------------------------

def test_tool_result_to_tool_message():
    r = ToolResult(True, {"duration": 60.0})
    msg = r.to_tool_message()
    assert msg["role"] == "tool"
    assert json.loads(msg["content"]) == {"duration": 60.0}


# --- System prompt -----------------------------------------------------------

def test_editing_system_prompt_mentions_tools():
    assert "tools" in EDITING_SYSTEM_PROMPT.lower()
    assert "probe_video" in EDITING_SYSTEM_PROMPT


def test_refinement_instructions_mentions_preserve():
    assert "preserv" in REFINEMENT_INSTRUCTIONS.lower()