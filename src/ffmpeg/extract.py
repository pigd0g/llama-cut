from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .timestamp import (
    build_final_filename,
    even_subsample_indices,
    parse_showinfo_stderr,
)


# --- Strategies -------------------------------------------------------------

STRATEGY_FPS_2S = "fps_2s"            # 0-60s
STRATEGY_SCENE = "scene"              # 1-10min
STRATEGY_KEYFRAME = "keyframe"        # 10-30min
STRATEGY_THUMBNAIL = "thumbnail"      # 30min+
STRATEGY_FPS_5S = "fps_5s"            # fallback for scene
STRATEGY_FPS_10S = "fps_10s"          # final fallback

# Target counts for the named modes
MODE_TARGETS = {"quick": 30, "standard": 60, "detailed": 80}
SUBSAMPLE_CAP = 100
SUBSAMPLE_TARGET = 80


@dataclass
class StrategyDecision:
    strategy: str
    label: str
    command_filter: str
    target_count: Optional[int] = None
    extra_input_args: list[str] = field(default_factory=list)
    needs_vsync_vfr: bool = False


def _scale_filter() -> str:
    return "scale='min(1280,iw)':-2"


def select_dynamic_strategy(duration: float, fps: float) -> StrategyDecision:
    """Pick the Dynamic extraction strategy based on duration."""
    if duration <= 0:
        # Unknown duration: treat as short
        duration = 30.0

    if duration <= 60.0:
        flt = f"fps=1/2,{_scale_filter()}"
        return StrategyDecision(
            strategy=STRATEGY_FPS_2S,
            label="1 frame every 2s",
            command_filter=flt,
            target_count=int(duration / 2) + 1,
        )
    if duration <= 600.0:  # 1-10 min
        flt = f"select='gt(scene,0.3)',{_scale_filter()}"
        return StrategyDecision(
            strategy=STRATEGY_SCENE,
            label="Scene detection (threshold 0.3)",
            command_filter=flt,
            needs_vsync_vfr=True,
        )
    if duration <= 1800.0:  # 10-30 min
        flt = _scale_filter()
        return StrategyDecision(
            strategy=STRATEGY_KEYFRAME,
            label="Keyframe extraction",
            command_filter=flt,
            needs_vsync_vfr=True,
            extra_input_args=["-skip_frame", "nokey"],
        )
    # 30min+
    total_frames = int(duration * (fps or 24.0))
    seg = max(1, total_frames // 60)
    flt = f"thumbnail={seg},{_scale_filter()}"
    return StrategyDecision(
        strategy=STRATEGY_THUMBNAIL,
        label="Thumbnail filter",
        command_filter=flt,
        needs_vsync_vfr=True,
    )


def select_fixed_strategy(mode: str, duration: float, custom_count: int) -> StrategyDecision:
    """Build a strategy that targets a fixed frame count via fps=1/N."""
    if mode == "custom":
        target = max(1, int(custom_count))
    else:
        target = MODE_TARGETS.get(mode, 60)
    if duration <= 0:
        duration = 30.0
    # fps that yields roughly `target` frames over `duration`
    interval = duration / target if target else duration
    interval = max(0.1, interval)
    flt = f"fps=1/{interval:.4f},{_scale_filter()}"
    return StrategyDecision(
        strategy=f"fixed_{mode}",
        label=f"~{target} frames (1 every {interval:.1f}s)",
        command_filter=flt,
        target_count=target,
    )


def select_strategy(settings_mode: str, duration: float, fps: float,
                    custom_count: int) -> StrategyDecision:
    if settings_mode == "dynamic":
        return select_dynamic_strategy(duration, fps)
    return select_fixed_strategy(settings_mode, duration, custom_count)


# Fallback chain: scene -> keyframe -> thumbnail -> fps_5s -> fps_10s
FALLBACK_CHAIN = {
    STRATEGY_SCENE: STRATEGY_KEYFRAME,
    STRATEGY_KEYFRAME: STRATEGY_THUMBNAIL,
    STRATEGY_THUMBNAIL: STRATEGY_FPS_5S,
    STRATEGY_FPS_5S: STRATEGY_FPS_10S,
    STRATEGY_FPS_2S: STRATEGY_FPS_5S,  # short-video failure path
    STRATEGY_FPS_10S: None,
}


def fallback_for(strategy: str) -> Optional[str]:
    return FALLBACK_CHAIN.get(strategy)


def decision_from_strategy(strategy: str, duration: float, fps: float) -> StrategyDecision:
    """Rebuild a StrategyDecision for a fallback strategy name."""
    if strategy == STRATEGY_FPS_5S:
        flt = f"fps=1/5,{_scale_filter()}"
        return StrategyDecision(strategy, "1 frame every 5s (fallback)", flt,
                                target_count=int(duration / 5) + 1 if duration > 0 else None)
    if strategy == STRATEGY_FPS_10S:
        flt = f"fps=1/10,{_scale_filter()}"
        return StrategyDecision(strategy, "1 frame every 10s (fallback)", flt,
                                target_count=int(duration / 10) + 1 if duration > 0 else None)
    if strategy == STRATEGY_KEYFRAME:
        return StrategyDecision(STRATEGY_KEYFRAME, "Keyframe extraction (fallback)",
                                _scale_filter(), needs_vsync_vfr=True,
                                extra_input_args=["-skip_frame", "nokey"])
    if strategy == STRATEGY_THUMBNAIL:
        total_frames = int(duration * (fps or 24.0))
        seg = max(1, total_frames // 60)
        return StrategyDecision(STRATEGY_THUMBNAIL, "Thumbnail filter (fallback)",
                                f"thumbnail={seg},{_scale_filter()}", needs_vsync_vfr=True)
    # default: fps_2s
    return StrategyDecision(STRATEGY_FPS_2S, "1 frame every 2s (fallback)",
                            f"fps=1/2,{_scale_filter()}")


# --- Command building -------------------------------------------------------

def _ffmpeg_bin() -> str:
    p = shutil.which("ffmpeg")
    return p if p else "ffmpeg"


def build_ffmpeg_command(
    video_path: str | Path,
    out_dir: Path,
    decision: StrategyDecision,
    video_stem: str,
) -> list[str]:
    """Build the ffmpeg command. We append showinfo to capture pts_time from
    stderr in a single pass."""
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_pattern = str(out_dir / f"{video_stem}_%04d.jpg")
    cmd: list[str] = [_ffmpeg_bin(), "-hide_banner", "-y"]
    cmd += list(decision.extra_input_args)
    cmd += ["-i", str(video_path)]
    vf = decision.command_filter
    # Append showinfo AFTER scale so pts_time reflects the written frame.
    cmd += ["-vf", f"{vf},showinfo"]
    if decision.needs_vsync_vfr:
        cmd += ["-vsync", "vfr"]
    cmd += ["-q:v", "5", temp_pattern]
    return cmd


# --- Extraction result ------------------------------------------------------

@dataclass
class ExtractionOutcome:
    video_path: str
    video_stem: str
    strategy_used: str
    strategy_label: str
    frames: list[dict] = field(default_factory=list)  # {path, pts_time, index, filename}
    failed: bool = False
    error: str = ""

    def frame_count(self) -> int:
        return len(self.frames)


# --- Runner -----------------------------------------------------------------

def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,
            check=False, encoding="utf-8", errors="replace",
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def _list_temp_frames(out_dir: Path, stem: str) -> list[Path]:
    pat = re.compile(rf"^{re.escape(stem)}_(\d+)\.jpg$")
    files = []
    for p in out_dir.iterdir():
        if pat.match(p.name):
            files.append(p)
    files.sort(key=lambda p: p.name)
    return files


def _rename_and_record(
    out_dir: Path, stem: str, pts_times: list[float],
    video_path: str, strategy: str,
) -> list[dict]:
    """Rename temp_NNNN.jpg -> final timestamp filename. Return frame records."""
    temp_files = _list_temp_frames(out_dir, stem)
    records: list[dict] = []
    for i, tf in enumerate(temp_files):
        pts = pts_times[i] if i < len(pts_times) else 0.0
        final_name = build_final_filename(stem, pts, i + 1)
        final_path = out_dir / final_name
        try:
            if final_path.exists():
                final_path.unlink()
            tf.rename(final_path)
        except OSError:
            # fall back: keep temp name if rename fails
            final_path = tf
            final_name = tf.name
        records.append({
            "path": str(final_path),
            "filename": final_name,
            "video_path": video_path,
            "video_stem": stem,
            "pts_time": pts,
            "index": i + 1,
            "strategy": strategy,
        })
    return records


def _cleanup_remaining_temp(out_dir: Path, stem: str) -> None:
    for tf in _list_temp_frames(out_dir, stem):
        try:
            tf.unlink()
        except OSError:
            pass


def extract_frames(
    video_path: str,
    video_stem: str,
    duration: float,
    fps: float,
    out_dir: Path,
    decision: StrategyDecision,
    progress_cb: Optional[Callable[[str], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> ExtractionOutcome:
    """Run one extraction pass with the given decision, applying the rules:
    - 0 frames from scene -> retry at fps_5s
    - 0 frames or non-zero return -> fall back along FALLBACK_CHAIN
    - >100 frames -> subsample to 80 (rename + delete the rest)
    Returns an ExtractionOutcome with the final frame list.
    """
    outcome = ExtractionOutcome(
        video_path=video_path, video_stem=video_stem,
        strategy_used=decision.strategy, strategy_label=decision.label,
    )

    current = decision
    attempts: list[str] = []
    while True:
        if is_cancelled and is_cancelled():
            outcome.failed = True
            outcome.error = "cancelled"
            return outcome
        attempts.append(current.strategy)
        if progress_cb:
            progress_cb(f"Trying {current.label}...")
        cmd = build_ffmpeg_command(video_path, out_dir, current, video_stem)
        rc, _stdout, stderr = _run(cmd)
        pts_times = parse_showinfo_stderr(stderr)
        temp_files = _list_temp_frames(out_dir, video_stem)
        count = len(temp_files)

        if rc == 0 and count > 0:
            # success — record strategy used
            outcome.strategy_used = current.strategy
            outcome.strategy_label = current.label
            # subsample if >100
            if count > SUBSAMPLE_CAP:
                keep = set(even_subsample_indices(count, SUBSAMPLE_TARGET))
                # delete non-kept temp files
                for i, tf in enumerate(temp_files):
                    if i not in keep:
                        try:
                            tf.unlink()
                        except OSError:
                            pass
                # recompute pts_times to the kept set
                temp_files = _list_temp_frames(out_dir, video_stem)
                kept_idxs = [i for i in range(count) if i in keep]
                pts_times = [pts_times[i] if i < len(pts_times) else 0.0 for i in kept_idxs]
                # re-index kept frames to 1..N
                records = _rename_and_record(out_dir, video_stem, pts_times,
                                             video_path, current.strategy)
                outcome.frames = records
            else:
                records = _rename_and_record(out_dir, video_stem, pts_times,
                                             video_path, current.strategy)
                outcome.frames = records
            _cleanup_remaining_temp(out_dir, video_stem)
            return outcome

        # Failure path
        _cleanup_remaining_temp(out_dir, video_stem)
        next_strategy = fallback_for(current.strategy)
        if next_strategy is None:
            outcome.failed = True
            outcome.error = f"all strategies failed: {' -> '.join(attempts)}"
            return outcome
        if progress_cb:
            progress_cb(f"Fallback: {current.label} -> {next_strategy}")
        current = decision_from_strategy(next_strategy, duration, fps)