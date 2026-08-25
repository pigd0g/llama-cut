"""Tests for EditPlanExecutor and the ffmpeg command builders.

Covers the command builders (parametric: known args -> expected ffmpeg argv),
the executor flow (checkpoint reuse, partial-file cleanup, ffprobe caching,
fallback commands), and the path-safety / sanitize helpers touched by the
review fixes.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.edit_plan_executor import (
    CommandResult,
    EditPlanExecutor,
    ExecutorProgress,
    ValidateExpectations,
    _as_bool,
    _build_assemble_timeline,
    _build_create_edit,
    _build_create_transition,
    _build_extract_clip,
    _build_mix_audio,
    _build_render_video,
    _build_validate,
    _as_bool,
    _has_media_extension,
    _intermediate_encoder_args,
    _media_suffix,
    _parse_fps_expr,
    render_command_as_string,
)
from src.video_production import (
    EditCommand,
    EditPlan,
    _is_safe_output_path,
    _sanitize_name,
)


# --- Fixtures ----------------------------------------------------------------

@pytest.fixture
def ex(tmp_path: Path) -> EditPlanExecutor:
    """An executor rooted in a tmp working folder."""
    plan = EditPlan()
    return EditPlanExecutor(str(tmp_path), plan)


@pytest.fixture
def ex_with_clips(tmp_path: Path) -> EditPlanExecutor:
    """An executor with two intermediate clips already on disk (for probes)."""
    plan = EditPlan()
    ex = EditPlanExecutor(str(tmp_path), plan)
    # Create two fake clips in the clips dir.
    (ex._clips_dir / "shot01.mp4").write_bytes(b"fake")
    (ex._clips_dir / "shot02.mp4").write_bytes(b"fake")
    return ex


def _cmd(cmd_id: str, cmd_type: str, args: dict, beat_id: str | None = None) -> EditCommand:
    return EditCommand(id=cmd_id, type=cmd_type, beat_id=beat_id, args=args)


# --- _intermediate_encoder_args ----------------------------------------------

def test_intermediate_encoder_args_returns_list():
    args = _intermediate_encoder_args()
    assert isinstance(args, list)
    assert len(args) >= 6
    assert "-c:v" in args


def test_intermediate_encoder_args_includes_pix_fmt():
    args = _intermediate_encoder_args()
    assert "yuv420p" in args


def test_intermediate_encoder_args_nvenc_or_libx264():
    with patch("src.edit_plan_executor.is_nvenc_available", return_value=True):
        args = _intermediate_encoder_args()
        assert "h264_nvenc" in args
    with patch("src.edit_plan_executor.is_nvenc_available", return_value=False):
        args = _intermediate_encoder_args()
        assert "libx264" in args


# --- Output extension validation ----------------------------------------------

def test_has_media_extension():
    from pathlib import Path as P
    assert _has_media_extension(P("out.mp4"))
    assert _has_media_extension(P("out.MOV"))  # case-insensitive
    assert _has_media_extension(P("out.webm"))
    assert not _has_media_extension(P("out"))
    assert not _has_media_extension(P("out.txt"))
    assert not _has_media_extension(P("timeline_v2"))


def test_media_suffix():
    assert _media_suffix("final.mp4") == ".mp4"
    assert _media_suffix("final.MOV") == ".mov"
    assert _media_suffix("final") == ""
    assert _media_suffix("final.txt") == ""  # non-media extension not preserved


# --- _as_bool ----------------------------------------------------------------

def test_as_bool_accepts_string_variants():
    assert _as_bool("true") is True
    assert _as_bool("True") is True
    assert _as_bool("1") is True
    assert _as_bool("yes") is True
    assert _as_bool("false") is False
    assert _as_bool("no") is False
    assert _as_bool("0") is False
    assert _as_bool("") is False


def test_as_bool_accepts_primitives():
    assert _as_bool(True) is True
    assert _as_bool(False) is False
    assert _as_bool(1) is True
    assert _as_bool(0) is False
    assert _as_bool(None) is False


# --- _build_extract_clip ----------------------------------------------------

def test_extract_clip_uses_ss_and_t_not_to(ex: EditPlanExecutor):
    """Phase 1.7: -ss START + -t DURATION, not -to END (version-ambiguous)."""
    (ex._working_folder / "src.mp4").write_bytes(b"fake")
    cmd, out_path, ckpt, fb = _build_extract_clip(
        {"source": "src.mp4", "start_time": 5.0, "end_time": 12.0,
         "output_name": "clip1"}, ex,
    )
    assert "-ss" in cmd
    assert "-t" in cmd
    assert "-to" not in cmd
    ss_idx = cmd.index("-ss")
    t_idx = cmd.index("-t")
    i_idx = cmd.index("-i")
    # -ss comes before -i (fast seek), -t comes after -i.
    assert ss_idx < i_idx < t_idx
    # Duration = end - start = 7.0
    assert cmd[t_idx + 1] == "7.0"


def test_extract_clip_uses_intermediate_encoder(ex: EditPlanExecutor):
    """Phase 2.1/2.2: uses _intermediate_encoder_args, not hardcoded libx264 CRF 18."""
    (ex._working_folder / "src.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_extract_clip(
        {"source": "src.mp4", "start_time": 0, "end_time": 5,
         "output_name": "c"}, ex,
    )
    enc_args = _intermediate_encoder_args()
    # The command should contain the intermediate encoder args (at least the codec).
    assert enc_args[0] in cmd  # "-c:v"
    assert enc_args[1] in cmd  # the codec name


def test_extract_clip_output_path_in_clips_dir(ex: EditPlanExecutor):
    (ex._working_folder / "src.mp4").write_bytes(b"fake")
    cmd, out_path, ckpt, _fb = _build_extract_clip(
        {"source": "src.mp4", "start_time": 0, "end_time": 5,
         "output_name": "clipA"}, ex,
    )
    assert out_path == ex._clips_dir / "clipA.mp4"
    assert ckpt == out_path
    assert _fb is None


def test_extract_clip_returns_4_tuple(ex: EditPlanExecutor):
    (ex._working_folder / "src.mp4").write_bytes(b"fake")
    result = _build_extract_clip(
        {"source": "src.mp4", "start_time": 0, "end_time": 1,
         "output_name": "c"}, ex,
    )
    assert len(result) == 4


# --- _build_create_edit -----------------------------------------------------

def test_create_edit_applies_frame_rate(ex: EditPlanExecutor):
    """Phase 1.1: frame_rate must add fps=N to the -vf chain."""
    (ex._clips_dir / "in.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_create_edit(
        {"input_clip": "in", "output_name": "out", "frame_rate": 30}, ex,
    )
    assert "-vf" in cmd
    vf_idx = cmd.index("-vf")
    vf_str = cmd[vf_idx + 1]
    assert "fps=30" in vf_str


def test_create_edit_no_frame_rate_omits_fps(ex: EditPlanExecutor):
    (ex._clips_dir / "in.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_create_edit(
        {"input_clip": "in", "output_name": "out"}, ex,
    )
    # Without frame_rate, no fps= filter should be added.
    if "-vf" in cmd:
        vf_idx = cmd.index("-vf")
        assert "fps=" not in cmd[vf_idx + 1]


def test_create_edit_trim_uses_t_not_to(ex: EditPlanExecutor):
    """Phase 1.7: trim uses -ss + -t DURATION, not -to."""
    (ex._clips_dir / "in.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_create_edit(
        {"input_clip": "in", "output_name": "out",
         "trim": {"start": 2.0, "end": 8.0}}, ex,
    )
    assert "-ss" in cmd
    assert "-t" in cmd
    assert "-to" not in cmd
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "6.0"  # 8.0 - 2.0


def test_create_edit_frame_rate_first_in_vf(ex: EditPlanExecutor):
    """fps= should come before other filters so they operate at the target rate."""
    (ex._clips_dir / "in.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_create_edit(
        {"input_clip": "in", "output_name": "out", "frame_rate": 30,
         "scale": "1280x720"}, ex,
    )
    vf_idx = cmd.index("-vf")
    vf_str = cmd[vf_idx + 1]
    assert vf_str.index("fps=30") < vf_str.index("scale=")


def test_create_edit_returns_4_tuple(ex: EditPlanExecutor):
    (ex._clips_dir / "in.mp4").write_bytes(b"fake")
    result = _build_create_edit({"input_clip": "in", "output_name": "out"}, ex)
    assert len(result) == 4


# --- _build_create_transition -----------------------------------------------

def test_create_transition_xfade_uses_clip_a_duration(ex_with_clips: EditPlanExecutor):
    """2-clip xfade offset = dur_a - duration (correct for the single transition case)."""
    with patch.object(ex_with_clips, "_probe_clip_duration", return_value=10.0):
        cmd, _out, _ckpt, _fb = _build_create_transition(
            {"clip_a": "shot01", "clip_b": "shot02", "transition": "dissolve",
             "duration": 2.0, "output_name": "trans"}, ex_with_clips,
        )
    fc_idx = cmd.index("-filter_complex")
    fc = cmd[fc_idx + 1]
    assert "offset=8.0" in fc  # 10.0 - 2.0


def test_create_transition_cut_uses_concat(ex_with_clips: EditPlanExecutor):
    cmd, _out, _ckpt, _fb = _build_create_transition(
        {"clip_a": "shot01", "clip_b": "shot02", "transition": "cut",
         "duration": 0.0, "output_name": "trans"}, ex_with_clips,
    )
    fc_idx = cmd.index("-filter_complex")
    assert "concat=n=2:v=1:a=0" in cmd[fc_idx + 1]


def test_create_transition_returns_4_tuple(ex_with_clips: EditPlanExecutor):
    result = _build_create_transition(
        {"clip_a": "shot01", "clip_b": "shot02", "transition": "cut",
         "duration": 0, "output_name": "t"}, ex_with_clips,
    )
    assert len(result) == 4


# --- _build_assemble_timeline (Phase 1.2 — the big fix) ----------------------

def test_assemble_timeline_xfade_offset_accumulates(ex_with_clips: EditPlanExecutor):
    """Phase 1.2: chained xfade offsets must use ACCUMULATED output duration,
    not individual clip durations.

    With 3 clips of duration 10s each and 2 dissolve transitions of 2s:
      - transition 1 (clip 0 -> 1): offset = 10 - 2 = 8   (accumulated was 10)
      - after transition 1: accumulated = 10 + 10 - 2 = 18
      - transition 2 (clip 1 -> 2): offset = 18 - 2 = 16  (NOT 10 - 2 = 8)
    """
    durations = {"shot01": 10.0, "shot02": 10.0, "shot03": 10.0}

    def fake_probe(name, *a, **kw):
        return durations.get(name, 10.0)

    with patch.object(ex_with_clips, "_probe_clip_duration", side_effect=fake_probe):
        cmd, _out, _ckpt, _fb = _build_assemble_timeline(
            {"clips": ["shot01", "shot02", "shot03"],
             "transitions": [
                 {"type": "dissolve", "duration": 2.0},
                 {"type": "dissolve", "duration": 2.0},
             ],
             "output_name": "timeline"}, ex_with_clips,
        )
    fc_idx = cmd.index("-filter_complex")
    fc = cmd[fc_idx + 1]
    # First xfade: offset=8.0 (10 - 2)
    # Second xfade: offset=16.0 (18 - 2, accumulated)
    assert "offset=8.0" in fc
    assert "offset=16.0" in fc
    # The OLD bug would have produced offset=8.0 for BOTH transitions.
    assert fc.count("offset=8.0") == 1


def test_assemble_timeline_xfade_offset_3_clips_varied(ex_with_clips: EditPlanExecutor):
    """Different durations to confirm accumulation is correct."""
    durations = {"shot01": 5.0, "shot02": 8.0, "shot03": 6.0}

    def fake_probe(name, *a, **kw):
        return durations.get(name, 10.0)

    with patch.object(ex_with_clips, "_probe_clip_duration", side_effect=fake_probe):
        cmd, _out, _ckpt, _fb = _build_assemble_timeline(
            {"clips": ["shot01", "shot02", "shot03"],
             "transitions": [
                 {"type": "dissolve", "duration": 1.0},
                 {"type": "dissolve", "duration": 1.0},
             ],
             "output_name": "tl"}, ex_with_clips,
        )
    fc = cmd[cmd.index("-filter_complex") + 1]
    # t1: offset = 5 - 1 = 4; accumulated = 5 + 8 - 1 = 12
    # t2: offset = 12 - 1 = 11
    assert "offset=4.0" in fc
    assert "offset=11.0" in fc


def test_assemble_timeline_no_transitions_returns_copy_fallback(ex_with_clips: EditPlanExecutor):
    """Phase 2.4: no-transitions path returns -c copy primary + re-encode fallback."""
    cmd, out_path, ckpt, fb = _build_assemble_timeline(
        {"clips": ["shot01", "shot02"], "output_name": "tl"}, ex_with_clips,
    )
    assert fb is not None
    assert "-c" in cmd and "copy" in cmd  # primary is -c copy
    # Fallback should be a re-encode (have -c:v with an encoder).
    assert "-c:v" in fb


def test_assemble_timeline_with_transitions_no_fallback(ex_with_clips: EditPlanExecutor):
    with patch.object(ex_with_clips, "_probe_clip_duration", return_value=10.0):
        cmd, _out, _ckpt, fb = _build_assemble_timeline(
            {"clips": ["shot01", "shot02"],
             "transitions": [{"type": "dissolve", "duration": 1.0}],
             "output_name": "tl"}, ex_with_clips,
        )
    assert fb is None  # transitions path has no fallback


def test_assemble_timeline_returns_4_tuple(ex_with_clips: EditPlanExecutor):
    result = _build_assemble_timeline(
        {"clips": ["shot01", "shot02"], "output_name": "tl"}, ex_with_clips,
    )
    assert len(result) == 4


# --- _build_mix_audio -------------------------------------------------------

def test_mix_audio_raises_on_unresolvable_audio(ex: EditPlanExecutor):
    """Phase 1.4: unresolvable audio source raises ValueError (no silent wrong-file fallback)."""
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0):
        with pytest.raises(ValueError, match="could not be resolved"):
            _build_mix_audio(
                {"video_clip": "video", "audio_sources": ["nonexistent_music"],
                 "volumes": [0.5]}, ex,
            )


def test_mix_audio_loops_short_music(ex: EditPlanExecutor):
    """Phase 1.6: short music gets -stream_loop -1 before its -i."""
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    # video is 30s, music is 10s -> should loop
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=10.0):
        cmd, _out, _ckpt, _fb = _build_mix_audio(
            {"video_clip": "video", "audio_sources": ["music"],
             "volumes": [0.5]}, ex,
        )
    assert "-stream_loop" in cmd
    loop_idx = cmd.index("-stream_loop")
    assert cmd[loop_idx + 1] == "-1"


def test_mix_audio_no_loop_when_music_long_enough(ex: EditPlanExecutor):
    """Phase 1.6: music longer than video does NOT get -stream_loop."""
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=60.0):
        cmd, _out, _ckpt, _fb = _build_mix_audio(
            {"video_clip": "video", "audio_sources": ["music"],
             "volumes": [0.5]}, ex,
        )
    assert "-stream_loop" not in cmd


def test_mix_audio_loop_false_no_looping(ex: EditPlanExecutor):
    """Phase 1.6: loop=False disables looping even for short music."""
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=30.0):
        cmd, _out, _ckpt, _fb = _build_mix_audio(
            {"video_clip": "video", "audio_sources": ["music"],
             "volumes": [0.5], "loop": False}, ex,
        )
    assert "-stream_loop" not in cmd


def test_mix_audio_loop_false_short_track_raises(ex: EditPlanExecutor):
    """loop=false with a track shorter than the edit must fail (not leave silence)."""
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=10.0):
        with pytest.raises(ValueError, match="shorter than the edit"):
            _build_mix_audio(
                {"video_clip": "video", "audio_sources": ["music"],
                 "volumes": [0.5], "loop": False}, ex,
            )


def test_mix_audio_loop_false_long_track_ok(ex: EditPlanExecutor):
    """loop=false with a track covering the edit builds normally."""
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=60.0):
        cmd, _out, _ckpt, _fb = _build_mix_audio(
            {"video_clip": "video", "audio_sources": ["music"],
             "volumes": [0.5], "loop": False}, ex,
        )
    assert "-stream_loop" not in cmd
    assert "-i" in cmd


def test_mix_audio_loop_string_variants(ex: EditPlanExecutor):
    """Boolean args arriving as JSON strings (e.g. 'false') are coerced correctly."""
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=10.0):
        # 'false' string must behave like False (no looping, and short track raises).
        with pytest.raises(ValueError, match="shorter than the edit"):
            _build_mix_audio(
                {"video_clip": "video", "audio_sources": ["music"],
                 "volumes": [0.5], "loop": "false"}, ex,
            )
        # 'true' string must behave like True (loop kicks in).
        cmd, _out, _ckpt, _fb = _build_mix_audio(
            {"video_clip": "video", "audio_sources": ["music"],
             "volumes": [0.5], "loop": "true"}, ex,
        )
    assert "-stream_loop" in cmd


def test_mix_audio_returns_4_tuple(ex: EditPlanExecutor):
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=30.0):
        result = _build_mix_audio(
            {"video_clip": "video", "audio_sources": ["music"],
             "volumes": [0.5]}, ex,
        )
    assert len(result) == 4


def test_mix_audio_amix_duration_first(ex: EditPlanExecutor):
    (ex._clips_dir / "video.mp4").write_bytes(b"fake")
    (ex._working_folder / "music.mp3").write_bytes(b"fake")
    with patch.object(ex, "_probe_clip_duration", return_value=30.0), \
         patch.object(ex, "_probe_clip_duration_from_path", return_value=30.0):
        cmd, _out, _ckpt, _fb = _build_mix_audio(
            {"video_clip": "video", "audio_sources": ["music"],
             "volumes": [0.5]}, ex,
        )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2:duration=first" in fc


# --- _build_render_video ----------------------------------------------------

def test_render_video_nvenc_when_available(ex: EditPlanExecutor):
    (ex._clips_dir / "timeline.mp4").write_bytes(b"fake")
    with patch("src.edit_plan_executor.is_nvenc_available", return_value=True):
        cmd, _out, _ckpt, _fb = _build_render_video(
            {"timeline": "timeline", "output_name": "final.mp4",
             "preset": "youtube_1080p"}, ex,
        )
    assert "h264_nvenc" in cmd


def test_render_video_libx264_when_no_nvenc(ex: EditPlanExecutor):
    (ex._clips_dir / "timeline.mp4").write_bytes(b"fake")
    with patch("src.edit_plan_executor.is_nvenc_available", return_value=False):
        cmd, _out, _ckpt, _fb = _build_render_video(
            {"timeline": "timeline", "output_name": "final.mp4",
             "preset": "youtube_1080p"}, ex,
        )
    assert "libx264" in cmd


def test_render_video_returns_4_tuple(ex: EditPlanExecutor):
    (ex._clips_dir / "timeline.mp4").write_bytes(b"fake")
    result = _build_render_video(
        {"timeline": "timeline", "output_name": "final.mp4"}, ex,
    )
    assert len(result) == 4
    assert result[3] is None  # no fallback for render


def test_render_video_extensionless_output_kept_extensionless(ex: EditPlanExecutor):
    """A render output_name without an extension is left extension-less;
    the executor's output-extension check then rejects it before ffmpeg runs."""
    (ex._clips_dir / "timeline.mp4").write_bytes(b"fake")
    cmd, out_path, _ckpt, _fb = _build_render_video(
        {"timeline": "timeline", "output_name": "final"}, ex,
    )
    assert out_path.name == "final"
    assert str(out_path) == cmd[-1]  # output path is the last argv element


def test_render_video_keeps_other_extensions(ex: EditPlanExecutor):
    (ex._clips_dir / "timeline.mp4").write_bytes(b"fake")
    _cmd_out, out_path, _ckpt, _fb = _build_render_video(
        {"timeline": "timeline", "output_name": "final.mov"}, ex,
    )
    assert out_path.name == "final.mov"


# --- _build_validate --------------------------------------------------------

def test_validate_returns_none_checkpoint(ex: EditPlanExecutor):
    """validate always runs (no checkpoint reuse)."""
    (ex._output_dir / "final.mp4").write_bytes(b"fake")
    cmd, out_path, ckpt, _fb = _build_validate(
        {"target": "final.mp4"}, ex,
    )
    assert out_path is None
    assert ckpt is None
    assert _fb is None


def test_validate_uses_json_output(ex: EditPlanExecutor):
    (ex._output_dir / "final.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_validate({"target": "final.mp4"}, ex)
    assert "-print_format" in cmd
    assert "json" in cmd


def test_validate_returns_4_tuple(ex: EditPlanExecutor):
    (ex._output_dir / "final.mp4").write_bytes(b"fake")
    result = _build_validate({"target": "final.mp4"}, ex)
    assert len(result) == 4


def test_validate_rejects_empty_target(ex: EditPlanExecutor):
    """An empty/missing target must fail with a clear error, not probe a
    directory (which would surface as a confusing 'Permission denied')."""
    with pytest.raises(ValueError, match="could not be resolved"):
        _build_validate({}, ex)


def test_validate_rejects_directory_target(ex: EditPlanExecutor):
    """Passing a folder (e.g. 'output') must fail before ffprobe runs."""
    (ex._output_dir / "final.mp4").write_bytes(b"fake")
    with pytest.raises(ValueError, match="could not be resolved"):
        _build_validate({"target": "output"}, ex)  # the output DIRECTORY


def test_validate_resolves_clip_by_name(ex_with_clips: EditPlanExecutor):
    """An extensionless intermediate clip name resolves to the clips dir."""
    cmd, _out, _ckpt, _fb = _build_validate(
        {"target": "shot01"}, ex_with_clips,
    )
    assert "shot01.mp4" in cmd[-1]


def test_validate_resolves_render_with_extension(ex: EditPlanExecutor):
    """The final render resolves by its output_name with extension."""
    (ex._output_dir / "final.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_validate({"target": "final.mp4"}, ex)
    assert cmd[-1].endswith("final.mp4")


def test_validate_resolves_extensionless_clip_in_clips_dir(ex: EditPlanExecutor):
    """A bare clip name that exists in the clips dir also resolves."""
    (ex._clips_dir / "timeline_v2.mp4").write_bytes(b"fake")
    cmd, _out, _ckpt, _fb = _build_validate({"target": "timeline_v2.mp4"}, ex)
    assert cmd[-1].endswith("timeline_v2.mp4")


# --- ValidateExpectations ---------------------------------------------------

def test_validate_expectations_from_args_defaults():
    e = ValidateExpectations.from_args({})
    assert e.expected_resolution == ""
    assert e.expected_fps == 0.0
    assert e.expect_audio is False


def test_validate_expectations_from_args():
    e = ValidateExpectations.from_args({
        "expected_resolution": "1920x1080",
        "expected_fps": 30.0,
        "expect_audio": True,
    })
    assert e.expected_resolution == "1920x1080"
    assert e.expected_fps == 30.0
    assert e.expect_audio is True


def test_validate_expectations_string_bools():
    """Agent-supplied string booleans are coerced ('false' means no audio)."""
    e = ValidateExpectations.from_args({"expect_audio": "false"})
    assert e.expect_audio is False
    e2 = ValidateExpectations.from_args({"expect_audio": "true"})
    assert e2.expect_audio is True


# --- _check_validate_output -------------------------------------------------

def test_check_validate_resolution_match(ex: EditPlanExecutor):
    ffprobe_out = json.dumps({
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                     "avg_frame_rate": "30/1"}],
    })
    e = ValidateExpectations(expected_resolution="1920x1080")
    ok, msg = ex._check_validate_output(ffprobe_out, e)
    assert ok
    assert msg == ""


def test_check_validate_resolution_mismatch(ex: EditPlanExecutor):
    ffprobe_out = json.dumps({
        "streams": [{"codec_type": "video", "width": 1280, "height": 720,
                     "avg_frame_rate": "30/1"}],
    })
    e = ValidateExpectations(expected_resolution="1920x1080")
    ok, msg = ex._check_validate_output(ffprobe_out, e)
    assert not ok
    assert "1280x720" in msg


def test_check_validate_fps_match(ex: EditPlanExecutor):
    ffprobe_out = json.dumps({
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                     "avg_frame_rate": "30000/1001"}],
    })
    e = ValidateExpectations(expected_fps=29.97)
    ok, msg = ex._check_validate_output(ffprobe_out, e)
    assert ok


def test_check_validate_audio_missing(ex: EditPlanExecutor):
    ffprobe_out = json.dumps({"streams": [{"codec_type": "video"}]})
    e = ValidateExpectations(expect_audio=True)
    ok, msg = ex._check_validate_output(ffprobe_out, e)
    assert not ok
    assert "audio" in msg.lower()


def test_check_validate_audio_present(ex: EditPlanExecutor):
    ffprobe_out = json.dumps({
        "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
    })
    e = ValidateExpectations(expect_audio=True)
    ok, _msg = ex._check_validate_output(ffprobe_out, e)
    assert ok


def test_check_validate_invalid_json(ex: EditPlanExecutor):
    ok, msg = ex._check_validate_output("not json", ValidateExpectations(expect_audio=True))
    assert not ok
    assert "json" in msg.lower()


# --- _parse_fps_expr --------------------------------------------------------

def test_parse_fps_expr_fractional():
    assert abs(_parse_fps_expr("30000/1001") - 29.97) < 0.1


def test_parse_fps_expr_simple():
    assert _parse_fps_expr("25/1") == 25.0


def test_parse_fps_expr_zero_zero():
    assert _parse_fps_expr("0/0") == 0.0


def test_parse_fps_expr_empty():
    assert _parse_fps_expr("") == 0.0


def test_parse_fps_expr_decimal():
    assert _parse_fps_expr("30.0") == 30.0


# --- Executor flow: checkpoint reuse ----------------------------------------

def test_checkpoint_reuse_skips_existing_output(tmp_path: Path):
    """A command whose output file already exists is skipped."""
    plan = EditPlan(commands=[
        _cmd("c1", "extract_clip",
             {"source": "src.mp4", "start_time": 0, "end_time": 5,
              "output_name": "clip"}),
    ])
    ex = EditPlanExecutor(str(tmp_path), plan)
    # Pre-create the output so the checkpoint check finds it.
    out = ex._clips_dir / "clip.mp4"
    out.write_bytes(b"already here")

    results = []
    # We don't want ffmpeg to actually run; the checkpoint check should skip it.
    with patch.object(ex, "_run_with_progress") as mock_run:
        success, failed, all_results = ex.run()
    assert success is True
    assert len(all_results) == 1
    assert all_results[0].skipped is True
    mock_run.assert_not_called()


# --- Executor flow: output extension enforcement -----------------------------

def test_executor_rejects_extensionless_output(tmp_path: Path):
    """Any command whose builder produced an extension-less output fails fast."""
    plan = EditPlan(commands=[
        _cmd("c1", "render_video",
             {"timeline": "timeline", "output_name": "no_ext"}),  # no extension
    ])
    ex = EditPlanExecutor(str(tmp_path), plan)
    (ex._clips_dir / "timeline.mp4").write_bytes(b"fake")

    with patch.object(ex, "_run_with_progress") as mock_run:
        success, failed, results = ex.run()
    assert success is False
    assert failed is not None
    assert "media extension" in failed.error
    mock_run.assert_not_called()  # ffmpeg never ran


def test_executor_accepts_mp4_output(tmp_path: Path):
    """render_video with an explicit .mp4 output passes the extension check."""
    plan = EditPlan(commands=[
        _cmd("c1", "render_video",
             {"timeline": "timeline", "output_name": "final.mp4"}),
    ])
    ex = EditPlanExecutor(str(tmp_path), plan)
    (ex._clips_dir / "timeline.mp4").write_bytes(b"fake")

    def fake_run(ffmpeg_cmd, cmd, *a, **kw):
        (ex._output_dir / "final.mp4").write_bytes(b"done")
        return 0, "", ""
    with patch.object(ex, "_run_with_progress", side_effect=fake_run):
        success, _failed, _results = ex.run()
    assert success is True
    # And the .mp4 output was actually written.
    assert (ex._output_dir / "final.mp4").exists()


# --- Executor flow: partial-file cleanup on failure -------------------------

def test_partial_output_cleaned_on_failure(tmp_path: Path):
    """Phase 1.3: a failed command's partial output is deleted."""
    plan = EditPlan(commands=[
        _cmd("c1", "extract_clip",
             {"source": "src.mp4", "start_time": 0, "end_time": 5,
              "output_name": "clip"}),
    ])
    ex = EditPlanExecutor(str(tmp_path), plan)
    # Simulate ffmpeg failing and leaving a partial file.
    def fake_run(ffmpeg_cmd, cmd, *a, **kw):
        out = ex._clips_dir / "clip.mp4"
        out.write_bytes(b"partial")
        return 1, "", "error"
    with patch.object(ex, "_run_with_progress", side_effect=fake_run):
        success, failed, _results = ex.run()
    assert success is False
    assert failed is not None
    # The partial file must have been cleaned up.
    assert not (ex._clips_dir / "clip.mp4").exists()


def test_partial_output_cleaned_on_cancel(tmp_path: Path):
    """Phase 1.3: a cancel that lands mid-command cleans up partial output."""
    plan = EditPlan(commands=[
        _cmd("c1", "extract_clip",
             {"source": "src.mp4", "start_time": 0, "end_time": 5,
              "output_name": "clip"}),
    ])
    cancelled = {"flag": False}
    ex = EditPlanExecutor(str(tmp_path), plan,
                          is_cancelled=lambda: cancelled["flag"])
    (tmp_path / "src.mp4").write_bytes(b"fake")
    def fake_run(ffmpeg_cmd, cmd, *a, **kw):
        cancelled["flag"] = True  # user cancels mid-command
        out = ex._clips_dir / "clip.mp4"
        out.write_bytes(b"partial")
        return 1, "", "cancelled"
    with patch.object(ex, "_run_with_progress", side_effect=fake_run):
        success, failed, _results = ex.run()
    assert success is False
    # Cancel mid-run still reports the failure and cleans the partial file
    # so it can't be reused as a checkpoint.
    assert failed is not None
    assert not (ex._clips_dir / "clip.mp4").exists()


# --- Executor flow: fallback command ----------------------------------------

def test_fallback_runs_when_primary_fails(tmp_path: Path):
    """Phase 2.4: assemble_timeline no-transitions primary (-c copy) fails, fallback succeeds."""
    plan = EditPlan(commands=[
        _cmd("c1", "assemble_timeline",
             {"clips": ["shot01", "shot02"], "output_name": "tl"}),
    ])
    ex = EditPlanExecutor(str(tmp_path), plan)
    (ex._clips_dir / "shot01.mp4").write_bytes(b"fake")
    (ex._clips_dir / "shot02.mp4").write_bytes(b"fake")

    call_count = {"n": 0}
    def fake_run(ffmpeg_cmd, cmd, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Primary (-c copy) fails.
            return 1, "", "concat failed"
        # Fallback succeeds; write the output.
        out = ex._clips_dir / "tl.mp4"
        out.write_bytes(b"encoded")
        return 0, "", ""
    with patch.object(ex, "_run_with_progress", side_effect=fake_run):
        success, failed, _results = ex.run()
    assert success is True
    assert call_count["n"] == 2
    assert (ex._clips_dir / "tl.mp4").exists()


def test_fallback_not_tried_when_primary_cancelled(tmp_path: Path):
    """Phase 2.4: fallback is not attempted if the primary was cancelled."""
    plan = EditPlan(commands=[
        _cmd("c1", "assemble_timeline",
             {"clips": ["shot01", "shot02"], "output_name": "tl"}),
    ])
    cancelled = {"flag": False}
    ex = EditPlanExecutor(str(tmp_path), plan,
                          is_cancelled=lambda: cancelled["flag"])
    (ex._clips_dir / "shot01.mp4").write_bytes(b"fake")
    (ex._clips_dir / "shot02.mp4").write_bytes(b"fake")

    call_count = {"n": 0}
    def fake_run(ffmpeg_cmd, cmd, *a, **kw):
        call_count["n"] += 1
        # The primary fails AND the user cancels at the same moment.
        cancelled["flag"] = True
        out = ex._clips_dir / "tl.mp4"
        out.write_bytes(b"partial")
        return 1, "", "cancelled"
    with patch.object(ex, "_run_with_progress", side_effect=fake_run):
        success, _failed, _results = ex.run()
    assert success is False
    # Primary ran once; the fallback must NOT run after a cancel.
    assert call_count["n"] == 1
    # Partial output cleaned up.
    assert not (ex._clips_dir / "tl.mp4").exists()


# --- Executor flow: ffprobe caching ------------------------------------------

def test_probe_clip_duration_caches(tmp_path: Path):
    """Phase 2.3: _probe_clip_duration calls ffprobe once per clip, then caches."""
    plan = EditPlan()
    ex = EditPlanExecutor(str(tmp_path), plan)
    (ex._clips_dir / "clip.mp4").write_bytes(b"fake")

    call_count = {"n": 0}
    def fake_ffprobe(path):
        call_count["n"] += 1
        from src.ffmpeg.probe import ProbeResult
        return ProbeResult(duration=42.0, width=1920, height=1080,
                           codec="h264", fps=30.0, raw={})

    with patch("src.edit_plan_executor.run_ffprobe", new=fake_ffprobe) if False \
         else patch("src.ffmpeg.probe.run_ffprobe", new=fake_ffprobe):
        d1 = ex._probe_clip_duration("clip")
        d2 = ex._probe_clip_duration("clip")
    assert d1 == 42.0
    assert d2 == 42.0
    assert call_count["n"] == 1  # cached after first call


def test_probe_cache_keyed_by_path(tmp_path: Path):
    """Different clips probe separately; same clip probes once."""
    plan = EditPlan()
    ex = EditPlanExecutor(str(tmp_path), plan)
    (ex._clips_dir / "a.mp4").write_bytes(b"fake")
    (ex._clips_dir / "b.mp4").write_bytes(b"fake")

    seen_paths: list[str] = []
    def fake_ffprobe(path):
        seen_paths.append(str(path))
        from src.ffmpeg.probe import ProbeResult
        return ProbeResult(duration=10.0, width=0, height=0, codec="", fps=0.0, raw={})

    with patch("src.ffmpeg.probe.run_ffprobe", new=fake_ffprobe):
        ex._probe_clip_duration("a")
        ex._probe_clip_duration("b")
        ex._probe_clip_duration("a")  # cached
    assert len(seen_paths) == 2  # a, b (a's second call was cached)


# --- Executor flow: progress command_index (Phase 3.3) ----------------------

def test_progress_uses_correct_command_index(tmp_path: Path):
    """Phase 3.3: _run_with_progress emits the real command index, not 0."""
    plan = EditPlan(commands=[
        _cmd("c1", "extract_clip",
             {"source": "src.mp4", "start_time": 0, "end_time": 1, "output_name": "a"}),
        _cmd("c2", "extract_clip",
             {"source": "src.mp4", "start_time": 0, "end_time": 1, "output_name": "b"}),
    ])
    ex = EditPlanExecutor(str(tmp_path), plan)
    (tmp_path / "src.mp4").write_bytes(b"fake")

    emitted_indices: list[int] = []
    def progress_cb(p: ExecutorProgress):
        emitted_indices.append(p.command_index)

    ex2 = EditPlanExecutor(str(tmp_path), plan, progress_cb=progress_cb)

    def fake_run(ffmpeg_cmd, cmd, *a, **kw):
        # Write the output so the success path works.
        out_name = cmd.args.get("output_name")
        (ex2._clips_dir / f"{out_name}.mp4").write_bytes(b"done")
        return 0, "", ""
    with patch.object(ex2, "_run_with_progress", side_effect=fake_run):
        ex2.run()
    # We should see index 0 and index 1 in the emissions (not just 0s).
    assert 0 in emitted_indices
    assert 1 in emitted_indices


# --- render_command_as_string ----------------------------------------------

def test_render_command_as_string_returns_string(tmp_path: Path):
    """render_command_as_string handles the 4-tuple builders."""
    (tmp_path / "src.mp4").write_bytes(b"fake")
    plan = EditPlan(commands=[
        _cmd("c1", "extract_clip",
             {"source": "src.mp4", "start_time": 0, "end_time": 1,
              "output_name": "clip"}),
    ])
    ex = EditPlanExecutor(str(tmp_path), plan)
    s = render_command_as_string(plan.commands[0], ex)
    assert isinstance(s, str)
    assert "-ss" in s
    assert "-t" in s


def test_render_command_as_string_unknown_type():
    cmd = EditCommand(id="x", type="extract_clip", args={})  # valid type, empty args
    s = render_command_as_string(cmd)
    # Should not crash; returns a string (may be an error message).
    assert isinstance(s, str)


# --- Path safety: _is_safe_output_path (Phase 3.1) --------------------------

def test_is_safe_output_path_sibling_prefix_bypass(tmp_path: Path):
    """Phase 3.1: /tmp/project vs /tmp/project-evil must NOT pass."""
    base = tmp_path / "project"
    base.mkdir()
    evil = tmp_path / "project-evil"
    evil.mkdir()
    evil_file = evil / "clip.mp4"
    # The old startswith check would pass /tmp/project-evil as inside /tmp/project.
    assert _is_safe_output_path(evil_file, base) is False


def test_is_safe_output_path_inside_still_works(tmp_path: Path):
    base = tmp_path / "clips"
    base.mkdir()
    assert _is_safe_output_path(base / "clip.mp4", base) is True


def test_is_safe_output_path_nested_subdir(tmp_path: Path):
    base = tmp_path / "clips"
    sub = base / "sub"
    sub.mkdir(parents=True)
    assert _is_safe_output_path(sub / "clip.mp4", base) is True


def test_is_safe_output_path_completely_outside(tmp_path: Path):
    base = tmp_path / "clips"
    base.mkdir()
    other = tmp_path / "evil.mp4"
    assert _is_safe_output_path(other, base) is False


# --- _sanitize_name: extension stripping (Phase 3.2) ------------------------

def test_sanitize_name_strips_mp4():
    """Phase 3.2: trailing .mp4 is stripped to avoid double extensions."""
    assert _sanitize_name("shot01.mp4") == "shot01"
    assert _sanitize_name("shot01.mov") == "shot01"
    assert _sanitize_name("timeline.mkv") == "timeline"


def test_sanitize_name_keeps_non_media_extensions():
    assert _sanitize_name("shot01_v2") == "shot01_v2"
    assert _sanitize_name("shot01.config") == "shot01.config"


def test_sanitize_name_case_insensitive_extension():
    assert _sanitize_name("shot01.MP4") == "shot01"
    assert _sanitize_name("shot01.MOV") == "shot01"


def test_sanitize_name_strips_only_one_extension():
    assert _sanitize_name("shot01.mp4.mp4") == "shot01.mp4"


def test_sanitize_name_basic_still_works():
    assert _sanitize_name("clip_01") == "clip_01"
    assert _sanitize_name("my clip/name") == "my_clip_name"


def test_sanitize_name_strips_slashes():
    assert _sanitize_name("../evil") == ".._evil"


def test_sanitize_name_long_string():
    long = "a" * 200
    assert len(_sanitize_name(long)) == 120
