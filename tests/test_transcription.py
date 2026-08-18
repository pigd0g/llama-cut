from pathlib import Path

import pytest

from src.transcription import (
    SUPPORTED_MODELS,
    TranscriptionResult,
    TranscriptionSettings,
    compute_type_options_for_device,
    configure_cuda_runtime,
    cuda_runtime_error_hint,
    default_compute_type_for_device,
    default_settings_for_device,
    detect_ffmpeg_hwaccel,
    detect_hardware_accel,
    is_model_present,
    model_cache_dir,
    segments_to_markdown,
)


# --- Hardware acceleration detection ----------------------------------------

def test_detect_hardware_accel_cuda(monkeypatch):
    """cuda when ctranslate2 reports a device AND cuBLAS loads."""
    import ctranslate2
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(
        "src.transcription._probe_cublas_loadable", lambda: True
    )
    assert detect_hardware_accel() == "cuda"


def test_detect_hardware_accel_no_gpu(monkeypatch):
    """cpu when ctranslate2 reports zero devices."""
    import ctranslate2
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    assert detect_hardware_accel() == "cpu"


def test_detect_hardware_accel_cublas_missing(monkeypatch):
    """cpu when a GPU is present but the cuBLAS runtime won't load."""
    import ctranslate2
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(
        "src.transcription._probe_cublas_loadable", lambda: False
    )
    assert detect_hardware_accel() == "cpu"


def test_detect_hardware_accel_ctranslate2_import_fails(monkeypatch):
    """cpu if ctranslate2 cannot be imported."""
    import sys
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    assert detect_hardware_accel() == "cpu"


def test_detect_ffmpeg_hwaccel_cuda(monkeypatch):
    """Simulate ffmpeg -hwaccels output listing cuda."""
    import subprocess
    fake_stdout = (
        "Hardware acceleration methods:\n"
        "cuda\n"
        "vaapi\n"
        "dxva2\n"
        "qsv\n"
    )
    class FakeProc:
        returncode = 0
        stdout = fake_stdout
        stderr = ""
    def fake_run(cmd, **kw):
        return FakeProc()
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_ffmpeg_hwaccel() == "cuda"


def test_detect_ffmpeg_hwaccel_no_cuda(monkeypatch):
    """No cuda in the hwaccels list -> cpu."""
    import subprocess
    fake_stdout = (
        "Hardware acceleration methods:\n"
        "vaapi\n"
        "dxva2\n"
    )
    class FakeProc:
        returncode = 0
        stdout = fake_stdout
        stderr = ""
    def fake_run(cmd, **kw):
        return FakeProc()
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_ffmpeg_hwaccel() == "cpu"


def test_detect_ffmpeg_hwaccel_failure_returns_cpu(monkeypatch):
    """Non-zero return code -> cpu."""
    import subprocess
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "error"
    def fake_run(cmd, **kw):
        return FakeProc()
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_ffmpeg_hwaccel() == "cpu"


def test_detect_ffmpeg_hwaccel_timeout_returns_cpu(monkeypatch):
    import subprocess
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert detect_ffmpeg_hwaccel() == "cpu"


# --- CUDA runtime configuration ---------------------------------------------

def test_configure_cuda_runtime_is_idempotent():
    """Calling twice does not raise and does not corrupt PATH."""
    import os
    before = os.environ.get("PATH", "")
    configure_cuda_runtime()
    configure_cuda_runtime()
    # PATH may have grown but must remain a valid string
    assert isinstance(os.environ.get("PATH", ""), str)


def test_configure_cuda_runtime_no_nvidia_packages(monkeypatch):
    """No nvidia packages installed -> no-op, no raise."""
    monkeypatch.setattr(
        "src.transcription._nvidia_package_bin_dirs", lambda: []
    )
    import os
    before = os.environ.get("LD_LIBRARY_PATH", "")
    configure_cuda_runtime()
    assert os.environ.get("LD_LIBRARY_PATH", "") == before


# --- CUDA runtime error hint ------------------------------------------------

def test_cuda_runtime_error_hint_matches_cublas():
    msg = "Library cublas64_12.dll is not found or cannot be loaded"
    hint = cuda_runtime_error_hint(msg)
    assert hint is not None
    assert "cuBLAS" in hint or "CUDA" in hint or "CPU" in hint


def test_cuda_runtime_error_hint_matches_cudnn():
    msg = "cudnn64_9.dll not found"
    hint = cuda_runtime_error_hint(msg)
    assert hint is not None


def test_cuda_runtime_error_hint_unrelated_error():
    assert cuda_runtime_error_hint("disk full") is None


# --- Compute type options ---------------------------------------------------

def test_compute_type_options_for_cuda():
    opts = compute_type_options_for_device("cuda")
    assert "float16" in opts
    assert "int8_float16" in opts
    assert "float32" in opts


def test_compute_type_options_for_cpu():
    opts = compute_type_options_for_device("cpu")
    assert "int8" in opts
    assert "float32" in opts


def test_default_compute_type_for_cuda_is_float16():
    assert default_compute_type_for_device("cuda") == "float16"


def test_default_compute_type_for_cpu_is_int8():
    assert default_compute_type_for_device("cpu") == "int8"


# --- Default settings -------------------------------------------------------

def test_default_settings_for_cuda():
    s = default_settings_for_device("cuda")
    assert s.device == "cuda"
    assert s.compute_type == "float16"
    assert s.language == "en"
    assert s.enabled is False
    assert s.model == "large-v3"


def test_default_settings_for_cpu():
    s = default_settings_for_device("cpu")
    assert s.device == "cpu"
    assert s.compute_type == "int8"


# --- TranscriptionSettings --------------------------------------------------

def test_settings_defaults_match_spec():
    s = TranscriptionSettings()
    assert s.enabled is False
    assert s.model == "large-v3"
    assert s.language == "en"
    assert s.temperature == 0.0
    assert s.condition_on_previous_text is False
    assert s.no_speech_threshold == 0.6
    assert s.log_prob_threshold == -1.0
    assert s.vad_filter is True
    assert s.vad_threshold == 0.5
    assert s.vad_min_speech_duration_ms == 250
    assert s.vad_min_silence_duration_ms == 500
    assert s.vad_speech_pad_ms == 200


def test_settings_roundtrip():
    s = TranscriptionSettings(
        enabled=True, model="large-v3-turbo", device="cpu",
        compute_type="int8", temperature=0.2,
        no_speech_threshold=0.7, vad_threshold=0.6,
        vad_min_speech_duration_ms=300, vad_min_silence_duration_ms=600,
        vad_speech_pad_ms=250, condition_on_previous_text=True,
        selected_video_paths=["a.mp4", "b.mp4"],
    )
    d = s.to_dict()
    s2 = TranscriptionSettings.from_dict(d)
    assert s2 == s


def test_settings_vad_parameters():
    s = TranscriptionSettings()
    vp = s.vad_parameters()
    assert vp == {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 200,
    }


def test_supported_models():
    assert "large-v3" in SUPPORTED_MODELS
    assert "large-v3-turbo" in SUPPORTED_MODELS


# --- Model cache + presence -------------------------------------------------

def test_model_cache_dir(tmp_path):
    d = model_cache_dir(str(tmp_path))
    assert d == tmp_path / "models"


def test_is_model_present_false_for_missing_dir(tmp_path):
    assert is_model_present("large-v3", tmp_path / "models") is False


def test_is_model_present_false_for_empty_dir(tmp_path):
    (tmp_path / "models").mkdir()
    assert is_model_present("large-v3", tmp_path / "models") is False


def test_is_model_present_false_for_unknown_model(tmp_path):
    assert is_model_present("unknown-model", tmp_path / "models") is False


def test_is_model_present_true_when_model_bin_exists(tmp_path):
    cache = tmp_path / "models"
    # build the HF cache layout: models--Systran--faster-whisper-large-v3/snapshots/<rev>/
    base = cache / "models--Systran--faster-whisper-large-v3"
    snap = base / "snapshots" / "deadbeef"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"fake")
    assert is_model_present("large-v3", cache) is True


# --- Markdown formatter -----------------------------------------------------

def _make_result(segments=None, language="en", prob=0.98, duration=12.5):
    return TranscriptionResult(
        video_path="v", video_stem="v",
        segments=segments or [],
        detected_language=language,
        language_probability=prob,
        duration=duration,
    )


def test_segments_to_markdown_includes_header_and_metadata():
    r = _make_result(
        segments=[{"start": 0.0, "end": 5.0, "text": "Hello world."},
                  {"start": 5.0, "end": 12.0, "text": "Second segment."}],
    )
    md = segments_to_markdown(r)
    assert md.startswith("# Transcription")
    assert "Detected language: en (probability: 0.98)" in md
    assert "Duration: 12.5s" in md
    assert "## 00:00 — 00:05" in md
    assert "Hello world." in md
    assert "## 00:05 — 00:12" in md
    assert "Second segment." in md


def test_segments_to_markdown_no_speaker_labels():
    """Spec example shows Speaker 1: labels; we omit them (no diarization)."""
    r = _make_result(segments=[{"start": 0.0, "end": 1.0, "text": "x"}])
    md = segments_to_markdown(r)
    assert "Speaker 1" not in md
    assert "Speaker" not in md


def test_segments_to_markdown_empty_segments():
    r = _make_result(segments=[])
    md = segments_to_markdown(r)
    assert "# Transcription" in md
    assert "_No speech detected._" in md


def test_segments_to_markdown_hours_timestamp():
    r = _make_result(
        segments=[{"start": 3723.0, "end": 3728.0, "text": "long video"}],
        duration=0.0,
    )
    md = segments_to_markdown(r)
    assert "## 01:02:03 — 01:02:08" in md


def test_segments_to_markdown_no_metadata_when_empty():
    r = _make_result(segments=[], language="", prob=0.0, duration=0.0)
    md = segments_to_markdown(r)
    assert "Detected language" not in md
    assert "Duration" not in md


def test_segments_to_markdown_silence_segment():
    r = _make_result(segments=[{"start": 0.0, "end": 2.0, "text": ""}])
    md = segments_to_markdown(r)
    assert "_(silence)_" in md


# --- extract_audio command shape (mocked) -----------------------------------

def test_extract_audio_invokes_ffmpeg_pcm_s16le_16k_mono(monkeypatch):
    import subprocess
    captured = {}
    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()
    monkeypatch.setattr(subprocess, "run", fake_run)
    from src.transcription import extract_audio
    wav = Path("/tmp/test.wav")
    # create the wav so the existence check passes
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"fake")
    try:
        extract_audio("input.mp4", wav)
    finally:
        wav.unlink(missing_ok=True)
    cmd = captured["cmd"]
    assert Path(cmd[0]).name.lower().startswith("ffmpeg")
    assert "-vn" in cmd
    assert "pcm_s16le" in cmd
    assert "16000" in cmd
    assert "1" in cmd  # mono
    assert str(wav) in cmd