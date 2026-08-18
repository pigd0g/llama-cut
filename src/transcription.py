from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional


SUPPORTED_MODELS = ["large-v3", "large-v3-turbo"]

# Map our model names to HuggingFace repo IDs used by faster-whisper
MODEL_REPO_IDS = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "Systran/faster-whisper-large-v3-turbo",
}

# Files that must be present for a model to be considered fully downloaded.
# These are the typical CTranslate2-format files for a faster-whisper model.
_MODEL_REQUIRED_FILES = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
]

FFMPEG_SAMPLE_RATE = 16000

# App root = directory containing main.py (this file lives in src/)
_APP_ROOT = Path(__file__).resolve().parents[1]


# --- Settings ---------------------------------------------------------------

@dataclass
class TranscriptionSettings:
    enabled: bool = False
    model: str = "large-v3"
    device: str = "cuda"           # auto-detected, user-editable
    compute_type: str = "float16"  # cuda->float16, cpu->int8
    language: str = "en"          # locked for now
    temperature: float = 0.0
    condition_on_previous_text: bool = False
    no_speech_threshold: float = 0.6
    log_prob_threshold: float = -1.0
    vad_filter: bool = True
    vad_threshold: float = 0.5
    vad_min_speech_duration_ms: int = 250
    vad_min_silence_duration_ms: int = 500
    vad_speech_pad_ms: int = 200
    selected_video_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TranscriptionSettings":
        fields = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})

    def vad_parameters(self) -> dict:
        return {
            "threshold": self.vad_threshold,
            "min_speech_duration_ms": self.vad_min_speech_duration_ms,
            "min_silence_duration_ms": self.vad_min_silence_duration_ms,
            "speech_pad_ms": self.vad_speech_pad_ms,
        }


def default_settings_for_device(device: str) -> TranscriptionSettings:
    """Build default settings, picking compute_type from the detected device."""
    s = TranscriptionSettings()
    s.device = device
    s.compute_type = "float16" if device == "cuda" else "int8"
    return s


# --- Hardware acceleration detection ----------------------------------------

# Markers in ctranslate2/faster-whisper runtime errors that indicate the CUDA
# runtime libraries (cuBLAS / cuDNN) are missing or unusable.
_CUDA_RUNTIME_ERROR_MARKERS = (
    "not found or cannot be loaded",
    "cublas",
    "cudnn",
    "libiomp",
)


def _nvidia_package_bin_dirs() -> list[Path]:
    """Return DLL/.so directories shipped by the nvidia-* pip packages.

    Portable across Windows (nvidia/cublas/bin) and Linux (nvidia/cublas/lib).
    Returns only directories that actually exist.
    """
    dirs: list[Path] = []
    for subpkg in ("nvidia.cublas.bin", "nvidia.cublas.lib",
                   "nvidia.cudnn.bin", "nvidia.cudnn.lib",
                   "nvidia.cuda_nvrtc.bin", "nvidia.cuda_nvrtc.lib"):
        try:
            mod = __import__(subpkg, fromlist=["__path__"])
        except ImportError:
            continue
        for p in getattr(mod, "__path__", []):
            d = Path(p)
            if d.exists():
                dirs.append(d)
    return dirs


def configure_cuda_runtime() -> None:
    """Make the pip-installed CUDA 12 runtime libraries findable.

    Idempotent. Windows: registers DLL directories via os.add_dll_directory()
    and prepends to PATH. Linux: prepends to LD_LIBRARY_PATH (dlopen reads it
    at load time, so this is effective before ctranslate2 is imported).
    macOS: no-op (NVIDIA CUDA is not supported on macOS).
    """
    if sys.platform == "darwin":
        return
    dirs = _nvidia_package_bin_dirs()
    if not dirs:
        return
    if os.name == "nt":
        for d in dirs:
            add_dll = getattr(os, "add_dll_directory", None)
            if add_dll:
                try:
                    add_dll(str(d))
                except OSError:
                    pass
        os.environ["PATH"] = (
            os.pathsep.join(str(d) for d in dirs)
            + os.pathsep + os.environ.get("PATH", "")
        )
    else:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        parts = [str(d) for d in dirs]
        if existing:
            parts.append(existing)
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(parts)


def _probe_cublas_loadable() -> bool:
    """Try to dlopen the cuBLAS 12 library from the configured runtime dirs.

    ctranslate2.get_cuda_device_count() only reflects the driver; cuBLAS loads
    lazily at inference time (which is when the user's pipeline crashed). This
    probe forces the load early so we can detect a broken CUDA runtime.
    """
    if sys.platform == "darwin":
        return False
    configure_cuda_runtime()
    lib_name = "cublas64_12.dll" if os.name == "nt" else "libcublas.so.12"
    try:
        if os.name == "nt":
            ctypes.WinDLL(lib_name)
        else:
            ctypes.CDLL(lib_name)
        return True
    except OSError:
        # Try an absolute path from the nvidia package dirs as a fallback.
        for d in _nvidia_package_bin_dirs():
            candidate = d / lib_name
            if candidate.exists():
                try:
                    if os.name == "nt":
                        ctypes.WinDLL(str(candidate))
                    else:
                        ctypes.CDLL(str(candidate))
                    return True
                except OSError:
                    continue
        return False


def detect_hardware_accel(ffmpeg_bin: Optional[str] = None) -> str:
    """Detect the best device for faster_whisper/ctranslate2.

    Returns 'cuda' iff BOTH (a) ctranslate2 reports a CUDA-capable GPU and
    (b) the cuBLAS 12 runtime library actually loads. Otherwise 'cpu'.

    The ffmpeg-based check is intentionally NOT used here: ffmpeg listing
    'cuda' in -hwaccels says nothing about ctranslate2's CUDA runtime, and a
    machine can pass one while failing the other (the bug this fixes).
    """
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() <= 0:
            return "cpu"
    except Exception:
        return "cpu"
    return "cuda" if _probe_cublas_loadable() else "cpu"


def detect_ffmpeg_hwaccel(ffmpeg_bin: Optional[str] = None) -> str:
    """Run `ffmpeg -hide_banner -hwaccels`, parse output.

    Returns 'cuda' if cuda is present in the output, else 'cpu'. This only
    reflects ffmpeg's build capabilities; use detect_hardware_accel() for the
    Whisper/ctranslate2 device decision.
    """
    ff = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
    try:
        proc = subprocess.run(
            [ff, "-hide_banner", "-hwaccels"],
            capture_output=True, text=True, timeout=30,
            check=False, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "cpu"
    if proc.returncode != 0:
        return "cpu"
    # parse stdout: lines after "Hardware acceleration methods:" are method names
    text = (proc.stdout or "") + (proc.stderr or "")
    accel_methods = []
    saw_header = False
    for line in text.splitlines():
        s = line.strip().lower()
        if not s:
            continue
        if s.endswith("hardware acceleration methods:"):
            saw_header = True
            continue
        if saw_header and s and not s.endswith(":"):
            accel_methods.append(s)
    if "cuda" in accel_methods:
        return "cuda"
    return "cpu"


def compute_type_options_for_device(device: str) -> list[str]:
    if device == "cuda":
        return ["float16", "int8_float16", "float32"]
    return ["int8", "int8_float32", "float32"]


def default_compute_type_for_device(device: str) -> str:
    return "float16" if device == "cuda" else "int8"


# --- Model cache + presence -------------------------------------------------

def model_cache_dir() -> Path:
    """Return the application-wide models directory (<app_root>/models).

    Models are stored once at the application level so they are shared
    across all projects / working folders and only downloaded once.
    """
    return _APP_ROOT / "models"


def _model_dir(model: str, cache_dir: Path) -> Path:
    repo_id = MODEL_REPO_IDS.get(model, model)
    # huggingface uses a normalised folder name: models--<org>--<name>
    org, _, name = repo_id.partition("/")
    return cache_dir / f"models--{org}--{name}"


def is_model_present(model: str, cache_dir: Path) -> bool:
    """Check if the model is fully downloaded in cache_dir.

    Uses HuggingFace cache layout. Returns True iff the snapshot dir exists
    and contains the expected model files.
    """
    if not model or not cache_dir or not Path(cache_dir).exists():
        return False
    base = _model_dir(model, Path(cache_dir))
    if not base.exists():
        return False
    snapshots = base / "snapshots"
    if not snapshots.exists():
        return False
    # find any snapshot dir
    snap_dirs = [p for p in snapshots.iterdir() if p.is_dir()]
    if not snap_dirs:
        return False
    snap = snap_dirs[0]
    # check at least model.bin exists (the large required file)
    return (snap / "model.bin").exists() or (snap / "pytorch_model.bin").exists()


def download_model(model: str, cache_dir: Path,
                   progress_cb: Optional[Callable[[str], None]] = None) -> bool:
    """Download the model files into cache_dir via huggingface_hub.

    Emits coarse per-stage messages via progress_cb. Returns True on success,
    False on failure. Designed to run in a worker thread.
    """
    repo_id = MODEL_REPO_IDS.get(model)
    if not repo_id:
        if progress_cb:
            progress_cb(f"Unknown model: {model}")
        return False
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        if progress_cb:
            progress_cb("huggingface_hub not installed")
        return False
    cache_dir.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb(f"Downloading {model} ({repo_id})...")
        progress_cb("This may take a few minutes on slow connections.")
    try:
        snapshot_download(
            repo_id=repo_id,
            cache_dir=str(cache_dir),
            local_files_only=False,
        )
    except Exception as e:
        if progress_cb:
            progress_cb(f"Download failed: {e}")
        return False
    if progress_cb:
        progress_cb("Download complete.")
    return True


def cuda_runtime_error_hint(error: str) -> str | None:
    """If ``error`` looks like a missing CUDA runtime library, return a
    user-actionable hint. Otherwise return None.
    """
    low = error.lower()
    if any(m.lower() in low for m in _CUDA_RUNTIME_ERROR_MARKERS):
        return (
            "GPU runtime missing (cuBLAS 12 / cuDNN). "
            "Reinstall dependencies (python -m pip install -r requirements.txt) "
            "or switch Device to CPU in Advanced settings."
        )
    return None


def build_whisper(model: str, cache_dir: Path, device: str, compute_type: str):
    """Construct a faster_whisper WhisperModel with local_files_only=True.

    Raises if the model is not present in cache_dir.
    """
    configure_cuda_runtime()
    from faster_whisper import WhisperModel
    return WhisperModel(
        model,
        device=device,
        compute_type=compute_type,
        download_root=str(cache_dir),
        local_files_only=True,
    )


# --- Audio extraction -------------------------------------------------------

def extract_audio(video_path: str, wav_path: Path,
                  ffmpeg_bin: Optional[str] = None) -> None:
    """Extract mono 16kHz PCM WAV from a video using ffmpeg."""
    ff = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff, "-hide_banner", "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(FFMPEG_SAMPLE_RATE),
        "-ac", "1",
        str(wav_path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=1800,
        check=False, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not wav_path.exists():
        raise RuntimeError(
            f"ffmpeg audio extraction failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:300]}"
        )


# --- Transcription ----------------------------------------------------------

@dataclass
class TranscriptionResult:
    video_path: str
    video_stem: str
    segments: list[dict] = field(default_factory=list)  # {start, end, text}
    detected_language: str = ""
    language_probability: float = 0.0
    duration: float = 0.0
    failed: bool = False
    error: str = ""


def transcribe_audio(model, wav_path: Path,
                     settings: TranscriptionSettings) -> TranscriptionResult:
    """Run model.transcribe(...) with the given settings, return segments + metadata."""
    segments, info = model.transcribe(
        str(wav_path),
        beam_size=5,
        language=settings.language,
        temperature=settings.temperature,
        condition_on_previous_text=settings.condition_on_previous_text,
        no_speech_threshold=settings.no_speech_threshold,
        log_prob_threshold=settings.log_prob_threshold,
        vad_filter=settings.vad_filter,
        vad_parameters=settings.vad_parameters(),
    )
    out = []
    for seg in segments:
        out.append({
            "start": float(seg.start),
            "end": float(seg.end),
            "text": seg.text.strip(),
        })
    return TranscriptionResult(
        video_path="",
        video_stem="",
        segments=out,
        detected_language=getattr(info, "language", "") or "",
        language_probability=float(getattr(info, "language_probability", 0.0)),
        duration=float(getattr(info, "duration", 0.0)) or 0.0,
    )


# --- Markdown formatter -----------------------------------------------------

def _fmt_ts(t: float) -> str:
    """00:00:05 style timestamp for markdown headings."""
    if t < 0:
        t = 0.0
    total = int(t)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def segments_to_markdown(result: TranscriptionResult) -> str:
    """Format the transcription markdown document."""
    lines: list[str] = ["# Transcription", ""]
    if result.detected_language:
        prob = f"{result.language_probability:.2f}" if result.language_probability else "n/a"
        lines.append(f"Detected language: {result.detected_language} (probability: {prob})")
    if result.duration > 0:
        lines.append(f"Duration: {result.duration:.1f}s")
    if result.detected_language or result.duration > 0:
        lines.append("")
    if not result.segments:
        lines.append("_No speech detected._")
        lines.append("")
        return "\n".join(lines)
    for seg in result.segments:
        start = _fmt_ts(seg["start"])
        end = _fmt_ts(seg["end"])
        lines.append(f"## {start} — {end}")
        lines.append("")
        text = seg["text"].strip()
        lines.append(text if text else "_(silence)_")
        lines.append("")
    return "\n".join(lines)