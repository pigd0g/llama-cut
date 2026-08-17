import json

from src.ffmpeg.probe import parse_ffprobe_json, parse_probe_data
from src.state import ExtractSettings, Frame, Video, _sanitize_stem


def test_parse_probe_data_extracts_fields():
    data = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30/1",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "123.456", "size": "12345678"},
    }
    r = parse_probe_data(data, "x.mp4")
    assert r is not None
    assert r.duration == 123.456
    assert r.width == 1920
    assert r.height == 1080
    assert r.codec == "h264"
    assert abs(r.fps - 29.97) < 0.1


def test_parse_probe_handles_missing_duration():
    data = {"streams": [{"codec_type": "video", "width": 1280, "height": 720,
                         "avg_frame_rate": "25/1"}], "format": {}}
    r = parse_probe_data(data)
    assert r is not None
    assert r.duration == 0.0
    assert r.fps == 25.0


def test_parse_probe_handles_zero_fps():
    data = {"streams": [{"codec_type": "video", "avg_frame_rate": "0/0"}],
            "format": {"duration": "10"}}
    r = parse_probe_data(data)
    assert r is not None
    assert r.fps == 0.0


def test_parse_ffprobe_json_invalid_returns_none():
    assert parse_ffprobe_json("not json", "x") is None


def test_video_from_path_sets_fields(tmp_path):
    f = tmp_path / "My Video File.mp4"
    f.write_bytes(b"x")
    v = Video.from_path(f)
    assert v.name == "My Video File.mp4"
    assert v.stem == "My_Video_File"
    assert v.size_bytes == 1
    assert v.selected is False


def test_sanitize_stem_removes_special_chars():
    assert _sanitize_stem("a/b\\c:d*e?f") == "a_b_c_d_e_f"


def test_sanitize_stem_truncates():
    s = _sanitize_stem("a" * 200)
    assert len(s) == 120


def test_frame_roundtrip():
    f = Frame(path="p", filename="f.jpg", video_path="v", video_stem="s",
              pts_time=1.5, index=3, strategy="scene")
    d = f.to_dict()
    f2 = Frame.from_dict(d)
    assert f2 == f


def test_extract_settings_defaults():
    s = ExtractSettings()
    assert s.mode == "dynamic"
    assert s.custom_count == 60


def test_extract_settings_custom_roundtrip():
    s = ExtractSettings(mode="custom", custom_count=42)
    d = s.to_dict()
    s2 = ExtractSettings.from_dict(d)
    assert s2.mode == "custom"
    assert s2.custom_count == 42