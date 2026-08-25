"""EditPlanExecutor — runs queued ffmpeg commands sequentially.

Receives an EditPlan (beats + commands) from the chat-driven planning phase
and executes the commands in order. Progress is weighted by video/clip
duration per the stage weights in video_production.STAGE_WEIGHTS.

Features:
  * Duration-weighted progress (Extract 20%, Transitions 10%, Assemble 15%,
    Audio 5%, Render 40%, Validate 10%).
  * Safe abort: cancel() kills the current subprocess and halts before the
    next command. Completed clips remain as checkpoints.
  * Checkpoint reuse: a command whose output file already exists is skipped
    (status="skipped") so re-runs after a failure don't redo finished work.
  * Per-command stderr progress parsing (time=) for smooth sub-progress.
  * Failure reporting: returns the failed command + ffmpeg stderr so the
    chat agent can propose a fix.

The executor does NOT call the LLM. It is purely deterministic. On failure,
the caller (EditExecutorWorker) emits the error and the page feeds the
failure back to the chat agent for a fix proposal.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Callable

from . import paths
from .video_production import (
    CLIPS_SUBDIR,
    OUTPUT_SUBDIR,
    PRESET_PROFILES,
    STAGE_WEIGHTS,
    SUPPORTED_PRESETS,
    SUPPORTED_TRANSITIONS,
    EditCommand,
    EditPlan,
    _ffmpeg_bin,
    _is_safe_output_path,
    _run_ffmpeg,
    _sanitize_name,
    is_nvenc_available,
)


# --- Progress types ----------------------------------------------------------

from dataclasses import dataclass, field


# --- Intermediate encoder helper ---------------------------------------------
# Intermediates are re-encoded in the final render, so prefer a fast preset.
# Uses the same NVENC automatic fallback as the final render (see AGENTS.md):
# if h264_nvenc is available, use it; otherwise libx264 ultrafast.

def _intermediate_encoder_args() -> list[str]:
    """Return encoder + args for an intermediate clip (fast, re-encodable).

    Returns a list suitable for splicing into an ffmpeg command after the
    filter/output args, e.g. ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "18"].
    """
    if is_nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "18",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p"]


# --- Output extension validation ---------------------------------------------
# Every ffmpeg output must carry a media extension (.mp4 etc.). The agent is
# told this in the system prompt, and the executor enforces it here so an
# extension-less output can never silently produce a file that downstream
# discovery (find_rendered_video, validate) cannot find.

_ALLOWED_OUTPUT_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _has_media_extension(p: Path) -> bool:
    """True if the path's suffix is a known media container extension."""
    return p.suffix.lower() in _ALLOWED_OUTPUT_EXTS


def _media_suffix(name: str) -> str:
    """Return the media-extension suffix of a raw name ('' if none).

    Used to keep a container extension out of _sanitize_name's stripping
    path for render outputs; the executor rejects extension-less names via
    the output-extension check in _run_command.
    """
    if _has_media_extension(Path(name)):
        return Path(name).suffix.lower()
    return ""


def _as_bool(value: object, default: bool = False) -> bool:
    """Robust boolean coercion for agent-supplied args (may be JSON strings)."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return default


# --- Validate expectations ---------------------------------------------------

@dataclass
class ValidateExpectations:
    """Expected properties for the final output, checked by validate."""
    expected_resolution: str = ""   # "WIDTHxHEIGHT" e.g. "1920x1080"
    expected_fps: float = 0.0       # 0 = don't check
    expect_audio: bool = False      # if True, require an audio stream

    @classmethod
    def from_args(cls, args: dict) -> "ValidateExpectations":
        return cls(
            expected_resolution=str(args.get("expected_resolution", "") or ""),
            expected_fps=float(args.get("expected_fps", 0.0) or 0.0),
            expect_audio=_as_bool(args.get("expect_audio", False)),
        )


@dataclass
class ExecutorProgress:
    """Cumulative progress reported by the executor."""
    overall: float = 0.0          # 0.0 → 1.0
    stage: str = ""               # current stage label
    stage_progress: float = 0.0   # 0.0 → 1.0 within the current stage
    command_id: str = ""          # current command id
    command_index: int = 0        # 0-based index of the current command
    command_total: int = 0        # total commands to run
    message: str = ""             # human-readable status line


@dataclass
class CommandResult:
    """Result of running a single command."""
    command: EditCommand
    success: bool
    output_path: str = ""
    error: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    skipped: bool = False


# --- Executor ----------------------------------------------------------------

class EditPlanExecutor:
    """Execute the commands in an EditPlan sequentially with weighted progress.

    The executor resolves clip names to paths in the clips directory,
    constructs ffmpeg commands from the typed EditCommand specs, runs them
    via subprocess, and reports progress via the progress_cb callback.

    Cancel is cooperative: the caller calls cancel() from another thread;
    the executor checks the flag between commands and kills the current
    subprocess.
    """

    def __init__(
        self,
        working_folder: str,
        plan: EditPlan,
        progress_cb: Callable[[ExecutorProgress], None] | None = None,
        log_cb: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._working_folder = Path(working_folder)
        self._video_dir = paths.video_dir(self._working_folder)
        self._clips_dir = self._video_dir / CLIPS_SUBDIR
        self._output_dir = self._video_dir / OUTPUT_SUBDIR
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._plan = plan
        self._progress_cb = progress_cb
        self._log_cb = log_cb
        self._is_cancelled = is_cancelled or (lambda: False)
        self._current_proc: subprocess.Popen | None = None
        self._intermediate_clips: dict[str, str] = {}
        self._probe_cache: dict[str, float] = {}  # resolved path str -> duration
        self._current_cmd_index: int = 0  # set per-iteration for progress emission

    def run(self) -> tuple[bool, CommandResult | None, list[CommandResult]]:
        """Execute all commands in the plan.

        Returns (success, failed_result_or_None, all_results).
        On success, all commands ran (or were skipped as checkpoints).
        On failure, failed_result describes the first failing command.
        On cancel, returns (False, None, results_so_far).
        """
        results: list[CommandResult] = []
        commands = self._plan.commands
        total = len(commands)
        completed_work = 0.0
        total_work = self._compute_total_work(commands)

        for i, cmd in enumerate(commands):
            if self._is_cancelled():
                self._log("Aborted by user.")
                return False, None, results

            self._current_cmd_index = i
            stage = self._stage_for_command(cmd)
            self._emit(ExecutorProgress(
                overall=completed_work / total_work if total_work > 0 else 0.0,
                stage=stage,
                stage_progress=0.0,
                command_id=cmd.id,
                command_index=i,
                command_total=total,
                message=f"{stage}: {cmd.type} ({cmd.id})",
            ))

            result = self._run_command(cmd, stage, completed_work, total_work)
            results.append(result)

            if result.skipped:
                self._log(f"[skip] {cmd.id} ({cmd.type}) — output already exists")
                completed_work += self._command_work(cmd)
                continue

            if not result.success:
                # Halt on failure. Return the failed command for LLM feedback.
                completed_work += self._command_work(cmd)
                self._emit(ExecutorProgress(
                    overall=completed_work / total_work if total_work > 0 else 0.0,
                    stage=stage,
                    stage_progress=1.0,
                    command_id=cmd.id,
                    command_index=i,
                    command_total=total,
                    message=f"FAILED: {cmd.type} ({cmd.id})",
                ))
                self._log(f"[fail] {cmd.id} ({cmd.type}): {result.error[:200]}")
                return False, result, results

            completed_work += self._command_work(cmd)
            self._emit(ExecutorProgress(
                overall=completed_work / total_work if total_work > 0 else 0.0,
                stage=stage,
                stage_progress=1.0,
                command_id=cmd.id,
                command_index=i,
                command_total=total,
                message=f"done: {cmd.type} ({cmd.id})",
            ))

        self._emit(ExecutorProgress(
            overall=1.0,
            stage="complete",
            stage_progress=1.0,
            command_id="",
            command_index=total,
            command_total=total,
            message="All commands complete.",
        ))
        return True, None, results

    def cancel(self) -> None:
        """Request cancellation. Kills the current subprocess if running."""
        if self._current_proc is not None:
            try:
                self._current_proc.terminate()
                time.sleep(0.5)
                if self._current_proc.poll() is None:
                    self._current_proc.kill()
            except Exception:
                pass

    # --- Command dispatch ---------------------------------------------------

    def _run_command(self, cmd: EditCommand, stage: str,
                     completed_work: float, total_work: float) -> CommandResult:
        """Run a single command, returning the result.

        Builders return a 4-tuple:
          (ffmpeg_cmd, out_path, checkpoint_path, fallback_cmd)
        fallback_cmd is None except for assemble_timeline (no transitions),
        which tries -c copy concat first then falls back to re-encode.
        """
        start = time.time()
        try:
            # validate is a special case: run ffprobe + parse + check expectations.
            if cmd.type == "validate":
                return self._run_validate(cmd, stage, completed_work, total_work, start)

            builder = _COMMAND_BUILDERS.get(cmd.type)
            if builder is None:
                return CommandResult(
                    cmd, False,
                    error=f"Unknown command type: {cmd.type}",
                    duration_s=time.time() - start,
                )
            ffmpeg_cmd, out_path, checkpoint_path, fallback_cmd = builder(cmd.args, self)

            # Output extension validation: every ffmpeg output must carry a
            # media extension. Without it the file may render but be invisible
            # to downstream discovery (find_rendered_video, validate).
            if out_path and not _has_media_extension(out_path):
                return CommandResult(
                    cmd, False,
                    error=("Output file must have a media extension (.mp4, .mov, .mkv, "
                           f".avi, .webm, .m4v) — got '{out_path.name}'. Add the "
                           "extension to the output_name."),
                    duration_s=time.time() - start,
                )

            # Checkpoint reuse: skip if the output already exists.
            if checkpoint_path is not None and checkpoint_path.exists():
                if out_path:
                    self._intermediate_clips[_sanitize_name(cmd.args.get("output_name", ""))] = str(out_path)
                return CommandResult(cmd, True, output_path=str(out_path or ""),
                                     skipped=True, duration_s=time.time() - start)

            if out_path and not _is_safe_output_path(out_path, self._working_folder):
                return CommandResult(cmd, False,
                                     error="Output path escaped the project directory",
                                     duration_s=time.time() - start)

            rc, stdout, stderr = self._run_with_progress(
                ffmpeg_cmd, cmd, stage, completed_work, total_work,
            )
            if rc != 0 and fallback_cmd is not None and not self._is_cancelled():
                # Primary failed; clean up any partial output before fallback.
                self._cleanup_partial(out_path)
                self._log(f"[fallback] {cmd.id} ({cmd.type}): primary failed, trying fallback")
                rc, stdout, stderr = self._run_with_progress(
                    fallback_cmd, cmd, stage, completed_work, total_work,
                )
            if rc != 0:
                # Clean up partial output on failure so checkpoint reuse can't
                # pick up a corrupt/truncated file on re-run.
                self._cleanup_partial(out_path)
                return CommandResult(
                    cmd, False,
                    output_path=str(out_path) if out_path else "",
                    error=f"ffmpeg exited with code {rc}",
                    stderr=stderr[:2000],
                    duration_s=time.time() - start,
                )
            if out_path and not Path(out_path).exists():
                return CommandResult(
                    cmd, False,
                    error="ffmpeg reported success but output file is missing",
                    stderr=stderr[:2000],
                    duration_s=time.time() - start,
                )

            if out_path:
                self._intermediate_clips[_sanitize_name(cmd.args.get("output_name", Path(out_path).stem))] = str(out_path)

            return CommandResult(
                cmd, True,
                output_path=str(out_path) if out_path else "",
                stderr=stderr[:500],
                duration_s=time.time() - start,
            )
        except Exception as e:
            return CommandResult(cmd, False, error=f"Executor error: {e}",
                                 duration_s=time.time() - start)

    def _run_validate(self, cmd: EditCommand, stage: str,
                      completed_work: float, total_work: float,
                      start: float) -> CommandResult:
        """Validate runs ffprobe and checks expected properties.

        The builder returns an ffprobe command; here we run it and verify
        expected_resolution, expected_fps, and expect_audio from cmd.args.
        Validation always runs (no checkpoint reuse).
        """
        from .video_production import _ffprobe_bin
        builder = _COMMAND_BUILDERS["validate"]
        ffmpeg_cmd, _out, _ckpt, _fb = builder(cmd.args, self)
        rc, stdout, stderr = self._run_with_progress(
            ffmpeg_cmd, cmd, stage, completed_work, total_work,
        )
        if rc != 0:
            return CommandResult(
                cmd, False,
                error=f"ffprobe exited with code {rc}",
                stderr=stderr[:2000],
                duration_s=time.time() - start,
            )
        expectations = ValidateExpectations.from_args(cmd.args)
        if expectations.expected_resolution or expectations.expected_fps or expectations.expect_audio:
            ok, message = self._check_validate_output(stdout, expectations)
            if not ok:
                return CommandResult(
                    cmd, False,
                    error=f"validation failed: {message}",
                    stderr=stderr[:2000],
                    duration_s=time.time() - start,
                )
        return CommandResult(
            cmd, True,
            output_path="",
            stderr=stderr[:500],
            duration_s=time.time() - start,
        )

    def _check_validate_output(self, ffprobe_stdout: str,
                               expectations: ValidateExpectations) -> tuple[bool, str]:
        """Parse ffprobe JSON stdout and verify it matches expectations."""
        try:
            data = json.loads(ffprobe_stdout)
        except json.JSONDecodeError:
            return False, "ffprobe output was not valid JSON"
        if expectations.expect_audio:
            has_audio = any(s.get("codec_type") == "audio"
                            for s in data.get("streams", []))
            if not has_audio:
                return False, "expected an audio stream but none found"
        if expectations.expected_fps:
            for s in data.get("streams", []):
                if s.get("codec_type") == "video":
                    fps_expr = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/0"
                    fps = _parse_fps_expr(fps_expr)
                    if fps <= 0:
                        return False, f"could not parse frame rate '{fps_expr}'"
                    if abs(fps - expectations.expected_fps) > 0.5:
                        return False, (f"frame rate {fps:.2f} does not match expected "
                                       f"{expectations.expected_fps}")
                    break
        if expectations.expected_resolution:
            try:
                ew, eh = (int(x) for x in expectations.expected_resolution.lower().split("x"))
            except (ValueError, AttributeError):
                return False, f"invalid expected_resolution '{expectations.expected_resolution}'"
            for s in data.get("streams", []):
                if s.get("codec_type") == "video":
                    w = int(s.get("width", 0))
                    h = int(s.get("height", 0))
                    if w != ew or h != eh:
                        return False, (f"resolution {w}x{h} does not match expected "
                                       f"{ew}x{eh}")
                    break
        return True, ""

    def _cleanup_partial(self, out_path: Path | None) -> None:
        """Delete a partial/truncated output file so it can't be reused as a checkpoint."""
        if out_path is None:
            return
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass

    def _run_with_progress(self, ffmpeg_cmd: list[str], cmd: EditCommand,
                           stage: str, completed_work: float,
                           total_work: float) -> tuple[int, str, str]:
        """Run an ffmpeg command with live stderr progress parsing.

        Parses `time=HH:MM:SS.ms` from ffmpeg stderr to compute sub-progress
        within the current command for smooth progress bar movement.
        """
        try:
            self._current_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as e:
            return 127, "", str(e)

        input_duration = self._command_input_duration(cmd)
        # Bound stderr accumulation: keep only the last 2000 lines (Phase 3.4).
        stderr_lines: deque[str] = deque(maxlen=2000)
        time_re = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")

        proc = self._current_proc
        assert proc is not None
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                stderr_lines.append(line)
                m = time_re.search(line)
                if m and input_duration > 0 and self._progress_cb:
                    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                    current = h * 3600 + mi * 60 + s
                    frac = min(1.0, current / input_duration)
                    cmd_work = self._command_work(cmd)
                    self._emit(ExecutorProgress(
                        overall=(completed_work + cmd_work * frac) / total_work if total_work > 0 else 0.0,
                        stage=stage,
                        stage_progress=frac,
                        command_id=cmd.id,
                        command_index=self._current_cmd_index,
                        command_total=len(self._plan.commands),
                        message=f"{stage}: {cmd.type} ({cmd.id}) {int(frac * 100)}%",
                    ))
        finally:
            # Wait for the process to finish and capture remaining output.
            remaining_stdout, remaining_stderr = proc.communicate()
            self._current_proc = None
            # Partial-output cleanup on cancel/failure is handled by
            # _run_command via _cleanup_partial (it sees the non-zero rc).

        rc = proc.returncode
        stdout = remaining_stdout or ""
        stderr = "".join(stderr_lines) + (remaining_stderr or "")
        return rc, stdout, stderr

    # --- Progress weighting -------------------------------------------------

    def _compute_total_work(self, commands: list[EditCommand]) -> float:
        """Compute the total weighted work across all commands."""
        total = 0.0
        for cmd in commands:
            total += self._command_work(cmd)
        return total

    def _command_work(self, cmd: EditCommand) -> float:
        """Compute the weighted work for a single command.

        Work = duration_weight × stage_weight, where duration_weight is the
        relevant video/clip duration in seconds (or 1.0 for count-based
        stages like validate).
        """
        stage = self._stage_for_command(cmd)
        weight = STAGE_WEIGHTS.get(stage, 0.05)
        duration = self._command_input_duration(cmd)
        return max(duration, 1.0) * weight

    def _stage_for_command(self, cmd: EditCommand) -> str:
        """Map a command type to its progress stage label."""
        type_to_stage = {
            "extract_clip": "extract",
            "create_edit": "extract",
            "create_transition": "transitions",
            "assemble_timeline": "assemble",
            "mix_audio": "audio",
            "render_video": "render",
            "validate": "validate",
        }
        return type_to_stage.get(cmd.type, "validate")

    def _command_input_duration(self, cmd: EditCommand) -> float:
        """Estimate the input duration for a command (for progress weighting)."""
        t = cmd.type
        args = cmd.args
        if t in ("extract_clip",):
            return max(0.1, args.get("end_time", 0) - args.get("start_time", 0))
        if t in ("create_edit",):
            trim = args.get("trim") or {}
            if trim:
                return max(0.1, trim.get("end", 0) - trim.get("start", 0))
            # Resolve the input clip's duration via probe.
            return self._probe_clip_duration(args.get("input_clip", ""))
        if t == "create_transition":
            return max(0.1, args.get("duration", 1.0)) * 2
        if t == "assemble_timeline":
            return self._sum_clip_durations(args.get("clips", []))
        if t == "mix_audio":
            return self._probe_clip_duration(args.get("video_clip", ""))
        if t == "render_video":
            return self._probe_clip_duration(args.get("timeline", "")) or self._plan.target_duration or 60.0
        if t == "validate":
            return 5.0  # validate is quick
        return 10.0

    def _probe_clip_duration(self, name: str) -> float:
        """Probe the duration of an intermediate clip by name (cached)."""
        p = self._resolve_clip(name)
        if p is None:
            return 10.0
        key = str(p)
        cached = self._probe_cache.get(key)
        if cached is not None:
            return cached
        try:
            from .ffmpeg.probe import run_ffprobe
            result = run_ffprobe(str(p))
            dur = result.duration if result else 10.0
        except Exception:
            dur = 10.0
        self._probe_cache[key] = dur
        return dur

    def _probe_clip_duration_from_path(self, p: Path) -> float:
        """Probe the duration of a file by Path (cached)."""
        key = str(p)
        cached = self._probe_cache.get(key)
        if cached is not None:
            return cached
        try:
            from .ffmpeg.probe import run_ffprobe
            result = run_ffprobe(str(p))
            dur = result.duration if result else 0.0
        except Exception:
            dur = 0.0
        self._probe_cache[key] = dur
        return dur

    def _sum_clip_durations(self, clip_names: list[str]) -> float:
        total = 0.0
        for name in clip_names:
            total += self._probe_clip_duration(name)
        return max(total, 1.0)

    # --- Path resolution ----------------------------------------------------

    def _resolve_clip(self, name: str) -> Path | None:
        """Resolve a clip/audio name to its file path."""
        if name in self._intermediate_clips:
            p = Path(self._intermediate_clips[name])
            if p.exists():
                return p
        from .state import AUDIO_EXTENSIONS
        for ext in (".mp4", ".mov", ".mkv", ".avi", ".webm") + tuple(AUDIO_EXTENSIONS):
            p = self._clips_dir / f"{name}{ext}"
            if p.exists():
                return p
        for ext in tuple(AUDIO_EXTENSIONS):
            p = self._working_folder / f"{name}{ext}"
            if p.exists():
                return p
        p = self._working_folder / name
        if p.exists() and p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
            return p
        return None

    def _resolve_source(self, name: str) -> Path | None:
        """Resolve a source video name to its path (in the working folder)."""
        p = self._working_folder / name
        if p.exists():
            return p
        return None

    # --- Callbacks ----------------------------------------------------------

    def _emit(self, progress: ExecutorProgress) -> None:
        if self._progress_cb:
            self._progress_cb(progress)

    def _log(self, msg: str) -> None:
        if self._log_cb:
            self._log_cb(msg)


# --- Command builders --------------------------------------------------------
# Each builder takes (args: dict, executor: EditPlanExecutor) and returns
# (ffmpeg_cmd: list[str], out_path: Path | None, checkpoint_path: Path | None).
# checkpoint_path is the file to check for reuse-skip (usually == out_path).

def _build_extract_clip(args: dict, ex: EditPlanExecutor):
    source = args.get("source", "")
    start = float(args.get("start_time", 0))
    end = float(args.get("end_time", 0))
    output_name = _sanitize_name(args.get("output_name", "clip"))
    src_path = ex._resolve_source(source) or ex._resolve_clip(source)
    if src_path is None:
        # Let the ffmpeg call fail naturally with a clear error.
        src_path = Path(source)
    out_path = ex._clips_dir / f"{output_name}.mp4"
    # Use -ss (input fast-seek) + -t DURATION (output option, version-unambiguous).
    # -to after -ss is version-dependent; -t is consistent across ffmpeg versions.
    duration = max(0.0, end - start)
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-y",
        "-ss", str(start),
        "-i", str(src_path),
        "-t", str(duration),
    ]
    cmd += _intermediate_encoder_args()
    cmd += ["-c:a", "aac", str(out_path)]
    return cmd, out_path, out_path, None


def _build_create_edit(args: dict, ex: EditPlanExecutor):
    input_clip = args.get("input_clip", "")
    output_name = _sanitize_name(args.get("output_name", "edit"))
    in_path = ex._resolve_clip(input_clip)
    if in_path is None:
        in_path = ex._clips_dir / f"{input_clip}.mp4"
    out_path = ex._clips_dir / f"{output_name}.mp4"

    trim = args.get("trim")
    speed = float(args.get("speed", 1.0))
    frame_rate = float(args.get("frame_rate", 0.0) or 0.0)
    crop = args.get("crop")
    scale = args.get("scale")
    aspect_ratio = args.get("aspect_ratio")
    color_adjustment = args.get("color_adjustment")
    audio_adjustment = args.get("audio_adjustment")

    vf_parts: list[str] = []
    af_parts: list[str] = []
    input_args: list[str] = []
    if trim:
        # -ss before -i (fast seek) + -t DURATION after -i (version-unambiguous).
        trim_start = float(trim.get("start", 0.0))
        trim_end = float(trim.get("end", 0.0))
        input_args = ["-ss", str(trim_start), "-i", str(in_path),
                      "-t", str(max(0.0, trim_end - trim_start))]
    else:
        input_args = ["-i", str(in_path)]
    # Apply fps normalisation first so downstream transitions/assembly see
    # a consistent frame rate (Phase 1.1; matches the system prompt's
    # "explicitly normalise its frame rate" rule).
    if frame_rate and frame_rate > 0:
        vf_parts.append(f"fps={frame_rate}")
    if speed and speed != 1.0:
        vf_parts.append(f"setpts={1.0/speed:.4f}*PTS")
        atempo = speed
        while atempo > 2.0:
            af_parts.append("atempo=2.0")
            atempo /= 2.0
        while atempo < 0.5:
            af_parts.append("atempo=0.5")
            atempo *= 2.0
        af_parts.append(f"atempo={atempo:.4f}")
    if crop:
        vf_parts.append(f"crop={crop}")
    if scale:
        vf_parts.append(f"scale={scale.replace('x', ':')}")
    if aspect_ratio:
        vf_parts.append(f"setdar={aspect_ratio}")
    if color_adjustment:
        eq_parts = []
        for k in ("brightness", "contrast", "saturation", "gamma"):
            if k in color_adjustment:
                eq_parts.append(f"{k}={color_adjustment[k]}")
        if eq_parts:
            vf_parts.append(f"eq={':'.join(eq_parts)}")
    if audio_adjustment:
        vol = audio_adjustment.get("volume", 1.0)
        if vol != 1.0:
            af_parts.append(f"volume={vol}")
        fi = audio_adjustment.get("fade_in", 0.0)
        fo = audio_adjustment.get("fade_out", 0.0)
        if fi > 0:
            af_parts.append(f"afade=t=in:d={fi}")
        if fo > 0:
            af_parts.append(f"afade=t=out:d={fo}")

    cmd = [_ffmpeg_bin(), "-hide_banner", "-y"] + input_args
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    cmd += _intermediate_encoder_args() + ["-c:a", "aac", str(out_path)]
    return cmd, out_path, out_path, None


def _build_create_transition(args: dict, ex: EditPlanExecutor):
    clip_a = args.get("clip_a", "")
    clip_b = args.get("clip_b", "")
    transition = args.get("transition", "cut")
    duration = float(args.get("duration", 1.0))
    output_name = _sanitize_name(args.get("output_name", "transition"))
    path_a = ex._resolve_clip(clip_a) or ex._clips_dir / f"{clip_a}.mp4"
    path_b = ex._resolve_clip(clip_b) or ex._clips_dir / f"{clip_b}.mp4"
    out_path = ex._clips_dir / f"{output_name}.mp4"

    if transition == "cut":
        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-y",
            "-i", str(path_a), "-i", str(path_b),
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v];"
            "[0:a][1:a]concat=n=2:v=0:a=1[a]",
            "-map", "[v]", "-map", "[a]",
        ]
        cmd += _intermediate_encoder_args() + ["-c:a", "aac", str(out_path)]
    else:
        dur_a = ex._probe_clip_duration(clip_a)
        offset = max(0.0, dur_a - duration)
        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-y",
            "-i", str(path_a), "-i", str(path_b),
            "-filter_complex",
            f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
            f"[0:a][1:a]acrossfade=d={duration}[a]",
            "-map", "[v]", "-map", "[a]",
        ]
        cmd += _intermediate_encoder_args() + ["-c:a", "aac", str(out_path)]
    return cmd, out_path, out_path, None


def _build_assemble_timeline(args: dict, ex: EditPlanExecutor):
    clips = args.get("clips", [])
    transitions = args.get("transitions", [])
    output_name = _sanitize_name(args.get("output_name", "timeline"))
    clip_paths = []
    for c in clips:
        p = ex._resolve_clip(c)
        if p is None:
            p = ex._clips_dir / f"{c}.mp4"
        clip_paths.append(p)
    out_path = ex._clips_dir / f"{output_name}.mp4"

    if transitions:
        filter_parts: list[str] = []
        input_args: list[str] = []
        for i, p in enumerate(clip_paths):
            input_args += ["-i", str(p)]
        for i in range(len(clip_paths)):
            filter_parts.append(
                f"[{i}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,fps=30[nv{i}]"
            )
            filter_parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[na{i}]")
        prev_v, prev_a = "[nv0]", "[na0]"
        # Track the accumulated OUTPUT stream duration as transitions are
        # chained (Phase 1.2). For xfade, the output of transition i is
        #   accumulated_dur + dur_i - t_dur_i
        # (the two clips overlap by t_dur_i). For concat (cut), it's additive:
        #   accumulated_dur + dur_i
        # Using individual clip durations for offsets is wrong for i>=2.
        accumulated_dur = ex._probe_clip_duration(clips[0]) if clips else 0.0
        for i in range(1, len(clip_paths)):
            trans = transitions[i-1] if i-1 < len(transitions) else {"type": "cut", "duration": 0.0}
            t_type = trans.get("type", "cut")
            t_dur = float(trans.get("duration", 0.0))
            is_last = i == len(clip_paths) - 1
            out_v = "[vout]" if is_last else f"[v{i}]"
            out_a = "[aout]" if is_last else f"[a{i}]"
            if t_type == "cut" or t_dur <= 0:
                filter_parts.append(
                    f"{prev_v}[nv{i}]concat=n=2:v=1:a=0{out_v};"
                    f"{prev_a}[na{i}]concat=n=2:v=0:a=1{out_a}"
                )
                accumulated_dur += ex._probe_clip_duration(clips[i])
            else:
                # xfade offset is the position in the ACCUMULATED output where
                # clip i begins to cross-fade in. That's:
                #   accumulated_dur - t_dur
                # (the last t_dur seconds of the accumulated stream overlap
                # with the first t_dur seconds of clip i).
                offset = max(0.0, accumulated_dur - t_dur)
                filter_parts.append(
                    f"{prev_v}[nv{i}]xfade=transition={t_type}:duration={t_dur}:offset={offset}{out_v};"
                    f"{prev_a}[na{i}]acrossfade=d={t_dur}{out_a}"
                )
                accumulated_dur += ex._probe_clip_duration(clips[i]) - t_dur
            prev_v, prev_a = out_v, out_a
        cmd = [_ffmpeg_bin(), "-hide_banner", "-y"] + input_args
        cmd += ["-filter_complex", ";".join(filter_parts)]
        cmd += ["-map", "[vout]", "-map", "[aout]"]
        cmd += _intermediate_encoder_args() + ["-c:a", "aac", str(out_path)]
        fallback_cmd = None
    else:
        # No transitions: try -c copy concat first (fast, lossless) with a
        # re-encode fallback (Phase 2.4). All clips come from the same extract
        # pipeline (libx264/aac/yuv420p), so -c copy should work when the agent
        # has normalised correctly.
        concat_list = ex._clips_dir / f"{output_name}_concat.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in clip_paths:
                f.write(f"file '{p}'\n")
        primary_cmd = [
            _ffmpeg_bin(), "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(out_path),
        ]
        fallback_cmd = [
            _ffmpeg_bin(), "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
        ]
        fallback_cmd += _intermediate_encoder_args() + ["-c:a", "aac", str(out_path)]
        cmd = primary_cmd
    return cmd, out_path, out_path, fallback_cmd


def _build_mix_audio(args: dict, ex: EditPlanExecutor):
    video_clip = args.get("video_clip", "")
    audio_sources = args.get("audio_sources", [])
    volumes = args.get("volumes", [])
    fades = args.get("fades")
    normalization = _as_bool(args.get("normalization", False))
    loop = _as_bool(args.get("loop", True))  # default: loop short music
    video_path = ex._resolve_clip(video_clip) or ex._clips_dir / f"{video_clip}.mp4"
    # Resolve audio sources; raise on unresolvable rather than loading a wrong
    # file (Phase 1.4 — was: `str(p or audio_sources[0])` which passed a bare
    # filename and could load the wrong file).
    audio_paths: list[Path] = []
    for a in audio_sources:
        p = ex._resolve_clip(a)
        if p is None:
            raise ValueError(
                f"mix_audio: audio source '{a}' could not be resolved to a file"
            )
        audio_paths.append(p)
    output_name = _sanitize_name(video_clip) + "_mixed"
    out_path = ex._clips_dir / f"{output_name}.mp4"

    # Phase 1.6: determine which audio sources need looping (shorter than
    # the video). Use -stream_loop -1 before the -i for those inputs.
    # With loop=false, a source shorter than the video is an error: the
    # doc promises the command fails rather than leaving silence (and the
    # agent gets the failure message back to propose a fix).
    video_dur = ex._probe_clip_duration(video_clip)
    input_args: list[str] = ["-i", str(video_path)]
    for p in audio_paths:
        audio_dur = ex._probe_clip_duration_from_path(p)
        if loop:
            if 0 < audio_dur < video_dur:
                input_args += ["-stream_loop", "-1"]
        else:
            if 0 < audio_dur < video_dur:
                raise ValueError(
                    f"mix_audio: audio source '{p.name}' is shorter than the "
                    f"edit ({audio_dur:.1f}s < {video_dur:.1f}s) and loop=false "
                    "was requested; the track must cover the full edit or be "
                    "looped (set loop: true)."
                )
        input_args += ["-i", str(p)]

    n_sources = len(audio_sources) + 1
    audio_labels = [f"[{i}:a]" for i in range(n_sources)]
    weights = " ".join(str(v) for v in [1.0] + volumes)
    filter_parts: list[str] = [
        f"{''.join(audio_labels)}amix=inputs={n_sources}:duration=first:weights={weights}[mixed]"
    ]
    fade_chain = "[mixed]"
    if fades:
        fi = fades.get("fade_in", 0.0)
        fo = fades.get("fade_out", 0.0)
        if fi > 0:
            filter_parts.append(f"[mixed]afade=t=in:d={fi}[faded]")
            fade_chain = "[faded]"
        if fo > 0:
            in_label = fade_chain
            fade_chain = "[norm]"
            filter_parts.append(f"{in_label}afade=t=out:d={fo}{fade_chain}")
    if normalization:
        in_label = fade_chain
        fade_chain = "[final_a]"
        filter_parts.append(f"{in_label}loudnorm=I=-16:TP=-1.5:LRA=11{fade_chain}")
    final_a_label = fade_chain if fade_chain != "[mixed]" else "[mixed]"

    cmd = [_ffmpeg_bin(), "-hide_banner", "-y"] + input_args
    cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", "0:v", "-map", final_a_label]
    cmd += ["-c:v", "copy", "-c:a", "aac", str(out_path)]
    return cmd, out_path, out_path, None


def _build_render_video(args: dict, ex: EditPlanExecutor):
    timeline = args.get("timeline", "timeline")
    output_name = _sanitize_name(args.get("output_name", "final.mp4"))
    resolution = args.get("resolution", "1920x1080")
    frame_rate = float(args.get("frame_rate", 30.0))
    video_codec = args.get("video_codec", "h264")
    audio_codec = args.get("audio_codec", "aac")
    preset = args.get("preset", "youtube_1080p")
    if preset not in SUPPORTED_PRESETS:
        preset = "youtube_1080p"
    in_path = ex._resolve_clip(timeline) or ex._clips_dir / f"{timeline}.mp4"
    # Preserve an explicit container extension on the output (the executor's
    # extension check then rejects a name with none). _sanitize_name strips
    # trailing media extensions, so capture the suffix first and re-append it.
    ext = _media_suffix(args.get("output_name", "final.mp4"))
    output_name = _sanitize_name(args.get("output_name", "final.mp4")) + ext
    out_path = ex._output_dir / output_name
    profile = PRESET_PROFILES[preset]
    vf_parts = [f"scale={resolution.replace('x', ':')}", f"fps={frame_rate}"]
    nvenc_available = is_nvenc_available()
    sw_vcodec = "libx264"
    hw_vcodec = "h264_nvenc"
    if video_codec == "h265":
        sw_vcodec = "libx265"
        hw_vcodec = "hevc_nvenc"
    vcodec = hw_vcodec if nvenc_available else sw_vcodec

    def _build(enc: str) -> list[str]:
        c = [_ffmpeg_bin(), "-hide_banner", "-y", "-i", str(in_path),
             "-vf", ",".join(vf_parts), "-c:v", enc]
        if enc.startswith(("h264_nvenc", "hevc_nvenc")):
            c += ["-preset", "p4", "-cq", profile["crf"]]
        elif enc == "libx264":
            c += ["-crf", profile["crf"], "-preset", profile["preset"]]
        elif enc == "libx265":
            c += ["-crf", str(int(profile["crf"]) + 5), "-preset", profile["preset"]]
        c += profile.get("extra", [])
        if "pix_fmt" in profile:
            c += ["-pix_fmt", profile["pix_fmt"]]
        c += ["-c:a", audio_codec, "-b:a", profile["abitrate"]]
        if profile.get("faststart"):
            c += ["-movflags", "+faststart"]
        c += [str(out_path)]
        return c

    cmd = _build(vcodec)
    return cmd, out_path, out_path, None


def _build_validate(args: dict, ex: EditPlanExecutor):
    """Validate is a probe + expectations check (see _run_validate).

    The builder produces the ffprobe command; _run_command dispatches
    validate commands to _run_validate, which runs ffprobe and checks
    expected_resolution, expected_fps, and expect_audio from args.

    `target` must name a real artifact file (a clip from this plan or the
    final render). An empty target, a folder, or an unresolvable name fails
    with a clear error — probing a directory would otherwise surface as a
    confusing "Permission denied" on Windows.
    """
    target = str(args.get("target", "") or "").strip()
    from .video_production import _ffprobe_bin
    p: Path | None = None
    if target:
        p = ex._resolve_clip(target)
        if p is None:
            # A render output lives in the output dir, usually with an
            # explicit extension (e.g. "final.mp4").
            cand = ex._output_dir / target
            if cand.is_file():
                p = cand
        if p is None:
            # A target that already carries an extension may be a clip name.
            cand = ex._clips_dir / target
            if cand.is_file():
                p = cand
    if p is None or not p.is_file() or not _has_media_extension(p):
        raise ValueError(
            f"validate: target '{target}' could not be resolved to a media "
            f"file (expected a prior artifact name — the render output_name "
            f"with its extension, or an extensionless intermediate clip "
            f"name; never a folder or directory path)"
        )
    cmd = [_ffprobe_bin(), "-hide_banner", "-show_format", "-show_streams",
           "-print_format", "json", str(p)]
    # No output file for validate; checkpoint = None so it always runs.
    return cmd, None, None, None


_COMMAND_BUILDERS = {
    "extract_clip": _build_extract_clip,
    "create_edit": _build_create_edit,
    "create_transition": _build_create_transition,
    "assemble_timeline": _build_assemble_timeline,
    "mix_audio": _build_mix_audio,
    "render_video": _build_render_video,
    "validate": _build_validate,
}


def render_command_as_string(cmd: EditCommand, executor: EditPlanExecutor | None = None) -> str:
    """Render an EditCommand as a copyable ffmpeg command string (for the debug modal)."""
    builder = _COMMAND_BUILDERS.get(cmd.type)
    if builder is None:
        return f"# unknown command type: {cmd.type}"
    try:
        ex = executor or EditPlanExecutor("", EditPlan(commands=[cmd]))
        ffmpeg_cmd, _out, _ckpt, _fb = builder(cmd.args, ex)
        return " ".join(_quote_arg(a) for a in ffmpeg_cmd)
    except Exception as e:
        return f"# error rendering command: {e}"


def _quote_arg(arg: str) -> str:
    """Quote a shell argument if it contains spaces or special chars."""
    if not arg:
        return "''"
    if any(c in arg for c in " \t\"'\\$`"):
        return f'"{arg}"'
    return arg


def _parse_fps_expr(expr: str) -> float:
    """Parse ffprobe frame-rate expressions like '30000/1001' or '25/1'."""
    if not expr or expr == "0/0":
        return 0.0
    try:
        if "/" in expr:
            num, den = expr.split("/", 1)
            n, d = float(num), float(den)
            return n / d if d else 0.0
        return float(expr)
    except (ValueError, ZeroDivisionError):
        return 0.0