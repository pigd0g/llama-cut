from __future__ import annotations

from unittest.mock import patch

import pytest

from src.video_metadata import (
    VideoMetadata,
    extract_metadata,
    metadata_to_markdown,
    metadata_to_markdown_all,
    parse_metadata,
    _format_bitrate,
    _format_duration_hms,
    _format_fps,
    _format_sample_rate,
    _parse_fps,
)


# --- Fixtures ----------------------------------------------------------------

def _sample_ffprobe_json(filename: str = "Holiday_001.mp4") -> dict:
    """Return a representative ffprobe JSON dict for testing."""
    return {
        "format": {
            "filename": filename,
            "duration": "522.040000",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "format_long_name": "QuickTime / MOV",
            "bit_rate": "15000000",
            "tags": {
                "major_brand": "mp42",
                "creation_time": "2025-01-15T10:30:00.000000Z",
            },
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "profile": "Main 10",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p",
                "display_aspect_ratio": "16:9",
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30000/1001",
                "bit_rate": "14000000",
                "tags": {"rotate": "0"},
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
                "bit_rate": "128000",
                "tags": {"language": "eng"},
            },
        ],
    }


def _minimal_ffprobe_json(filename: str = "clip.mp4") -> dict:
    """Minimal ffprobe JSON with only the essential fields."""
    return {
        "format": {
            "filename": filename,
            "duration": "10.0",
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "25/1",
            },
        ],
    }


def _no_audio_ffprobe_json() -> dict:
    """ffprobe JSON with video only (no audio stream)."""
    return {
        "format": {"duration": "5.0", "format_name": "mp4"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720,
             "avg_frame_rate": "30/1"},
        ],
    }


def _empty_streams_json() -> dict:
    """ffprobe JSON with no streams at all."""
    return {"format": {"duration": "0.0"}, "streams": []}


# --- parse_metadata tests ---------------------------------------------------

def test_parse_metadata_full_fields():
    data = _sample_ffprobe_json("Holiday_001.mp4")
    meta = parse_metadata(data, "Holiday_001.mp4")
    assert meta.source_filename == "Holiday_001.mp4"
    assert meta.source_path == "Holiday_001.mp4"
    assert meta.duration == pytest.approx(522.04)
    assert meta.duration_hms == "00:08:42"
    assert meta.container_format == "mov,mp4,m4a,3gp,3g2,mj2"
    assert meta.video_codec == "hevc"
    assert meta.video_profile == "Main 10"
    assert meta.width == 3840
    assert meta.height == 2160
    assert meta.frame_rate == pytest.approx(29.97002997, rel=1e-3)
    assert meta.avg_frame_rate_raw == "30000/1001"
    assert meta.pixel_format == "yuv420p"
    assert meta.aspect_ratio == "16:9"
    assert meta.video_bitrate == "14000000"
    assert meta.num_video_streams == 1
    assert meta.audio_codec == "aac"
    assert meta.audio_sample_rate == "48000"
    assert meta.audio_channels == 2
    assert meta.audio_channel_layout == "stereo"
    assert meta.audio_bitrate == "128000"
    assert meta.num_audio_streams == 1
    assert "major_brand" in meta.tags
    assert meta.tags["major_brand"] == "mp42"
    assert meta.raw is data


def test_parse_metadata_minimal_fields():
    data = _minimal_ffprobe_json("clip.mp4")
    meta = parse_metadata(data, "clip.mp4")
    assert meta.source_filename == "clip.mp4"
    assert meta.duration == pytest.approx(10.0)
    assert meta.duration_hms == "00:00:10"
    assert meta.video_codec == "h264"
    assert meta.width == 1920
    assert meta.height == 1080
    assert meta.frame_rate == pytest.approx(25.0)
    assert meta.video_profile == ""
    assert meta.pixel_format == ""
    assert meta.aspect_ratio == ""
    assert meta.video_bitrate == ""
    assert meta.audio_codec == ""
    assert meta.audio_sample_rate == ""
    assert meta.audio_channels == 0
    assert meta.num_audio_streams == 0


def test_parse_metadata_no_audio_stream():
    data = _no_audio_ffprobe_json()
    meta = parse_metadata(data, "silent.mp4")
    assert meta.video_codec == "h264"
    assert meta.num_audio_streams == 0
    assert meta.audio_codec == ""
    assert meta.audio_channels == 0
    assert meta.num_video_streams == 1


def test_parse_metadata_no_streams():
    data = _empty_streams_json()
    meta = parse_metadata(data, "empty.mp4")
    assert meta.num_video_streams == 0
    assert meta.num_audio_streams == 0
    assert meta.width == 0
    assert meta.height == 0
    assert meta.video_codec == ""
    assert meta.audio_codec == ""


def test_parse_metadata_tags_merged():
    data = _sample_ffprobe_json()
    meta = parse_metadata(data, "test.mp4")
    # Format-level tag
    assert "major_brand" in meta.tags
    assert meta.tags["major_brand"] == "mp42"
    # Stream-level tag (video)
    assert "rotate" in meta.tags
    # Stream-level tag (audio)
    assert "language" in meta.tags
    assert meta.tags["language"] == "eng"


def test_parse_metadata_empty_path():
    data = _minimal_ffprobe_json("")
    meta = parse_metadata(data, "")
    assert meta.source_filename == ""
    assert meta.source_path == ""


def test_parse_metadata_bad_duration():
    data = _minimal_ffprobe_json()
    data["format"]["duration"] = "not_a_number"
    meta = parse_metadata(data, "x.mp4")
    assert meta.duration == 0.0
    assert meta.duration_hms == "00:00:00"


# --- extract_metadata tests (mocked ffprobe) --------------------------------

def test_extract_metadata_returns_none_on_failure():
    with patch("src.video_metadata.run_ffprobe", return_value=None):
        assert extract_metadata("nonexistent.mp4") is None


def test_extract_metadata_success():
    from types import SimpleNamespace
    data = _sample_ffprobe_json("test.mp4")
    fake_result = SimpleNamespace(raw=data)
    with patch("src.video_metadata.run_ffprobe", return_value=fake_result):
        meta = extract_metadata("test.mp4")
    assert meta is not None
    assert meta.source_filename == "test.mp4"
    assert meta.video_codec == "hevc"


# --- metadata_to_markdown tests ---------------------------------------------

def test_metadata_to_markdown_full():
    data = _sample_ffprobe_json("Holiday_001.mp4")
    meta = parse_metadata(data, "Holiday_001.mp4")
    md = metadata_to_markdown(meta)
    assert md.startswith("## Holiday_001.mp4")
    assert "00:08:42" in md
    assert "3840 × 2160" in md
    assert "hevc" in md
    assert "Main 10" in md
    assert "yuv420p" in md
    assert "16:9" in md
    assert "aac" in md
    assert "48 kHz" in md
    assert "stereo" in md
    assert "Tags:" in md


def test_metadata_to_markdown_no_audio():
    data = _no_audio_ffprobe_json()
    meta = parse_metadata(data, "silent.mp4")
    md = metadata_to_markdown(meta)
    assert "## silent.mp4" in md
    assert "_(no audio stream)_" in md
    assert "Audio Streams: 0" in md


def test_metadata_to_markdown_missing_fields():
    meta = VideoMetadata(source_filename="empty.mp4")
    md = metadata_to_markdown(meta)
    assert "## empty.mp4" in md
    assert "_(unknown)_" in md
    assert "_(no audio stream)_" in md


def test_metadata_to_markdown_includes_source_path():
    meta = VideoMetadata(source_filename="test.mp4", source_path="/videos/test.mp4")
    md = metadata_to_markdown(meta)
    assert "/videos/test.mp4" in md


def test_metadata_to_markdown_includes_stream_counts():
    data = _sample_ffprobe_json()
    meta = parse_metadata(data, "test.mp4")
    md = metadata_to_markdown(meta)
    assert "Video Streams: 1" in md
    assert "Audio Streams: 1" in md


# --- metadata_to_markdown_all tests -----------------------------------------

def test_metadata_to_markdown_all_empty():
    assert metadata_to_markdown_all([]) == ""


def test_metadata_to_markdown_all_single():
    data = _sample_ffprobe_json("A.mp4")
    meta = parse_metadata(data, "A.mp4")
    md = metadata_to_markdown_all([meta])
    assert md.startswith("# Video Metadata")
    assert "## A.mp4" in md


def test_metadata_to_markdown_all_multiple():
    data1 = _sample_ffprobe_json("A.mp4")
    data2 = _minimal_ffprobe_json("B.mp4")
    meta1 = parse_metadata(data1, "A.mp4")
    meta2 = parse_metadata(data2, "B.mp4")
    md = metadata_to_markdown_all([meta1, meta2])
    assert "# Video Metadata" in md
    assert "## A.mp4" in md
    assert "## B.mp4" in md
    # A should appear before B
    assert md.index("## A.mp4") < md.index("## B.mp4")


# --- Helper function tests --------------------------------------------------

def test_parse_fps_valid():
    assert _parse_fps("30000/1001") == pytest.approx(29.97, rel=1e-2)
    assert _parse_fps("25/1") == 25.0
    assert _parse_fps("30/1") == 30.0
    assert _parse_fps("60") == 60.0


def test_parse_fps_invalid():
    assert _parse_fps("0/0") == 0.0
    assert _parse_fps("") == 0.0
    assert _parse_fps("not_a_number") == 0.0
    assert _parse_fps("1/0") == 0.0


def test_format_duration_hms():
    assert _format_duration_hms(0) == "00:00:00"
    assert _format_duration_hms(10.4) == "00:00:10"
    assert _format_duration_hms(10.6) == "00:00:11"
    assert _format_duration_hms(65) == "00:01:05"
    assert _format_duration_hms(3661) == "01:01:01"
    assert _format_duration_hms(-5) == "00:00:00"


def test_format_fps():
    assert _format_fps(0) == "0"
    assert _format_fps(25.0) == "25"
    assert _format_fps(29.97002997) == "29.97"
    assert _format_fps(59.94) == "59.94"


def test_format_bitrate():
    assert _format_bitrate("14000000") == "14 Mbps"
    assert _format_bitrate("128000") == "128 kbps"
    assert _format_bitrate("0") == "0"
    assert _format_bitrate("invalid") == "invalid"


def test_format_sample_rate():
    assert _format_sample_rate("48000") == "48 kHz"
    assert _format_sample_rate("44100") == "44 kHz"
    assert _format_sample_rate("8000") == "8 kHz"
    assert _format_sample_rate("invalid") == "invalid"