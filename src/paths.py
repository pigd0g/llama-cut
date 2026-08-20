from __future__ import annotations

from pathlib import Path
from typing import Union

# This module is the single source of truth for every application-generated
# path under the working folder. All app data lives under a single hidden
# root directory (``.llama-cut``) so the working folder root stays clean and
# contains only the user's raw media files.
#
# No other module should construct a ``.llama-cut`` path by hand — import the
# helpers here instead.

# The hidden root directory name. The leading dot hides it on macOS/Linux
# and signals "app internals" on Windows.
APP_ROOT_DIR = ".llama-cut"

# Subdirectory leaf names (kept here so the full tree is readable in one place).
CONTEXT_DIR_NAME = "context"
STORYBOARD_DIR_NAME = "storyboard"
FRAMES_DIR_NAME = "frames"
TRANSCRIPTION_DIR_NAME = "transcription"
VIDEO_DIR_NAME = "video"

# Files that live at the app root or in a known subfolder.
APP_STATE_FILENAME = "app_state.json"
FRAMES_INDEX_FILENAME = "frames.json"
THUMBS_DIR_NAME = ".thumbs"
CONTEXT_REPORT_FILENAME = "video_context_report.md"


PathLike = Union[str, Path]


def app_root(working_folder: PathLike) -> Path:
    """The hidden ``.llama-cut`` root directory for a working folder."""
    return Path(working_folder) / APP_ROOT_DIR


def context_dir(working_folder: PathLike) -> Path:
    """``.llama-cut/context`` — Markdown context files + manifest."""
    return app_root(working_folder) / CONTEXT_DIR_NAME


def storyboard_dir(working_folder: PathLike) -> Path:
    """``.llama-cut/storyboard`` — storyboard history + latest markdown."""
    return app_root(working_folder) / STORYBOARD_DIR_NAME


def frames_dir(working_folder: PathLike) -> Path:
    """``.llama-cut/frames`` — extracted frame images, frames.json, .thumbs."""
    return app_root(working_folder) / FRAMES_DIR_NAME


def transcription_dir(working_folder: PathLike) -> Path:
    """``.llama-cut/transcription`` — transient audio extracts (.wav)."""
    return app_root(working_folder) / TRANSCRIPTION_DIR_NAME


def video_dir(working_folder: PathLike) -> Path:
    """``.llama-cut/video`` — edit plan, tool log, clips, output, preview."""
    return app_root(working_folder) / VIDEO_DIR_NAME


def app_state_path(working_folder: PathLike) -> Path:
    """``.llama-cut/app_state.json`` — pipeline state checkpoint."""
    return app_root(working_folder) / APP_STATE_FILENAME


def frames_index_path(working_folder: PathLike) -> Path:
    """``.llama-cut/frames/frames.json`` — frame index checkpoint."""
    return frames_dir(working_folder) / FRAMES_INDEX_FILENAME


def thumbs_dir(working_folder: PathLike) -> Path:
    """``.llama-cut/frames/.thumbs`` — Stage 1 video thumbnails."""
    return frames_dir(working_folder) / THUMBS_DIR_NAME


def context_report_path(working_folder: PathLike) -> Path:
    """``.llama-cut/video_context_report.md`` — Stage 6 context review export."""
    return app_root(working_folder) / CONTEXT_REPORT_FILENAME