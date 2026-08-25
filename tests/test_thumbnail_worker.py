"""Tests for ThumbnailWorker: validation of cached thumbnails and
regeneration of corrupt/partial files.

The first video in the list used to stay blank: a superseded worker kept
writing its .jpg while a new worker wrote the same file (or a partial file
was left by a cancelled ffmpeg), and the worker trusted any existing
non-empty file. These tests lock in the fix.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.workers.thumbnail_worker import (
    ThumbnailWorker,
    _is_valid_thumbnail,
)


# --- Real-ffmpeg helpers -----------------------------------------------------

def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


@pytest.fixture
def sample_video(tmp_path: Path):
    """A tiny real mp4, or pytest.skip if ffmpeg is unavailable."""
    if not _have_ffmpeg():
        pytest.skip("ffmpeg not available")
    p = tmp_path / "sample.mp4"
    cmd = [
        shutil.which("ffmpeg"),
        "-y", "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=30",
        "-pix_fmt", "yuv420p", str(p),
    ]
    subprocess.run(cmd, capture_output=True, check=False)
    assert p.exists() and p.stat().st_size > 0
    return p


# --- _is_valid_thumbnail -----------------------------------------------------

def test_valid_jpeg_is_acceptable(tmp_path: Path):
    jpg = tmp_path / "ok.jpg"
    from PyQt6.QtGui import QImage
    img = QImage(64, 36, QImage.Format.Format_RGB32)
    img.fill(0xFF0000)
    assert img.save(str(jpg))
    assert _is_valid_thumbnail(jpg)


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "empty.jpg"
    p.write_bytes(b"")
    assert not _is_valid_thumbnail(p)


def test_garbage_file_rejected(tmp_path: Path):
    p = tmp_path / "garbage.jpg"
    p.write_bytes(b"this is not a jpeg" * 100)
    assert not _is_valid_thumbnail(p)


def test_missing_file_rejected(tmp_path: Path):
    assert not _is_valid_thumbnail(tmp_path / "nope.jpg")


def test_truncated_jpeg_rejected(tmp_path: Path):
    """A jpeg truncated mid-stream (the corruption a killed ffmpeg leaves)."""
    from PyQt6.QtGui import QColor, QImage
    jpg = tmp_path / "full.jpg"
    img = QImage(128, 72, QImage.Format.Format_RGB32)
    img.fill(QColor(0, 0, 0))
    assert img.save(str(jpg))
    data = jpg.read_bytes()
    jpg.write_bytes(data[: len(data) // 3])  # chop it
    assert not _is_valid_thumbnail(jpg)


# --- Worker generation -------------------------------------------------------

def test_generate_produces_valid_thumbnail(sample_video: Path, tmp_path: Path):
    thumbs = tmp_path / ".thumbs"
    worker = ThumbnailWorker([str(sample_video)], thumbs)
    result = worker._generate(str(sample_video))
    assert result is not None
    assert _is_valid_thumbnail(Path(result))


def test_generate_regenerates_corrupt_existing_thumbnail(sample_video: Path, tmp_path: Path):
    thumbs = tmp_path / ".thumbs"
    thumbs.mkdir(parents=True)
    # A corrupt non-empty file from a previous cancelled run.
    bad = thumbs / "sample.jpg"
    bad.write_bytes(b"junk" * 50)
    worker = ThumbnailWorker([str(sample_video)], thumbs)
    result = worker._generate(str(sample_video))
    assert result is not None
    # Same deterministic path, now valid.
    assert Path(result) == bad
    assert _is_valid_thumbnail(bad)


def test_generate_reuses_existing_valid_thumbnail(sample_video: Path, tmp_path: Path):
    thumbs = tmp_path / ".thumbs"
    worker = ThumbnailWorker([str(sample_video)], thumbs)
    first = worker._generate(str(sample_video))
    assert first is not None
    # Second call must reuse the cached file (no new decode).
    worker2 = ThumbnailWorker([str(sample_video)], thumbs)
    second = worker2._generate(str(sample_video))
    assert second == first
    assert _is_valid_thumbnail(Path(second))


def test_generate_missing_video_returns_none(tmp_path: Path):
    thumbs = tmp_path / ".thumbs"
    worker = ThumbnailWorker([str(tmp_path / "nope.mp4")], thumbs)
    assert worker._generate(str(tmp_path / "nope.mp4")) is None


# --- Serialised generation ---------------------------------------------------

def test_concurrent_generation_same_video_yields_valid_file(sample_video: Path, tmp_path: Path):
    """Two workers generating the same thumbnail at once (a superseded worker
    overlapping a fresh one) must not corrupt the file."""
    import threading

    thumbs = tmp_path / ".thumbs"
    results: list[str | None] = []
    errors: list[Exception] = []

    def gen():
        try:
            w = ThumbnailWorker([str(sample_video)], thumbs)
            results.append(w._generate(str(sample_video)))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=gen) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert all(r is not None for r in results)
    assert _is_valid_thumbnail(thumbs / "sample.jpg")
