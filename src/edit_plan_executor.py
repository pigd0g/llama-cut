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
from pathlib import Path
from typing import Callable

from . import paths
from .video_production import (
    CLIPS_SUBDIR,
    OUTPUT_SUBDIR,
    PRESET_PROFILES,
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
        """Run a single command, returning the result."""
        start = time.time()
        try:
            builder = _COMMAND_BUILDERS.get(cmd.type)
            if builder is None:
                return CommandResult(
                    cmd, False,
                    error=f"Unknown command type: {cmd.type}",
                    duration_s=time.time() - start,
                )
            ffmpeg_cmd, out_path, checkpoint_path = builder(cmd.args, self)

            # Checkpoint reuse: skip if the output already exists.
            if checkpoint_path is not None and checkpoint_path.exists():
                if out_path:
                    self._intermediate_clips[_sanitize_name(cmd.args.get("output_name", ""))] = str(out_path)
                return CommandResult(cmd, True, output_path=str(out_path or ""),
                                     skipped=True, duration_s=time.time() - start)

            if out_path and not _is_safe_output_path(Path(out_path),
                                                     out_path.parent if out_path else self._clips_dir):
                return CommandResult(cmd, False,
                                     error="Output path escaped the project directory",
                                     duration_s=time.time() - start)

            rc, stdout, stderr = self._run_with_progress(
                ffmpeg_cmd, cmd, stage, completed_work, total_work,
            )
            if rc != 0:
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
        stderr_lines: list[str] = []
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
                        command_index=0,
                        command_total=len(self._plan.commands),
                        message=f"{stage}: {cmd.type} ({cmd.id}) {int(frac * 100)}%",
                    ))
        finally:
            # Wait for the process to finish and capture remaining output.
            remaining_stdout, remaining_stderr = proc.communicate()
            self._current_proc = None

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
        from .video_production import STAGE_WEIGHTS
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
        """Probe the duration of an intermediate clip by name."""
        p = self._resolve_clip(name)
        if p is None:
            return 10.0
        try:
            from .ffmpeg.probe import run_ffprobe
            result = run_ffprobe(str(p))
            return result.duration if result else 10.0
        except Exception:
            return 10.0

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
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-y",
        "-ss", str(start),
        "-to", str(end),
        "-i", str(src_path),
        "-c:v", "libx264", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(out_path),
    ]
    return cmd, out_path, out_path


def _build_create_edit(args: dict, ex: EditPlanExecutor):
    input_clip = args.get("input_clip", "")
    output_name = _sanitize_name(args.get("output_name", "edit"))
    in_path = ex._resolve_clip(input_clip)
    if in_path is None:
        in_path = ex._clips_dir / f"{input_clip}.mp4"
    out_path = ex._clips_dir / f"{output_name}.mp4"

    trim = args.get("trim")
    speed = float(args.get("speed", 1.0))
    crop = args.get("crop")
    scale = args.get("scale")
    aspect_ratio = args.get("aspect_ratio")
    color_adjustment = args.get("color_adjustment")
    audio_adjustment = args.get("audio_adjustment")

    vf_parts: list[str] = []
    af_parts: list[str] = []
    input_args: list[str] = []
    if trim:
        input_args = ["-ss", str(trim.get("start", 0.0)), "-to", str(trim.get("end", 0.0))]
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

    cmd = [_ffmpeg_bin(), "-hide_banner", "-y"] + input_args + ["-i", str(in_path)]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    cmd += ["-c:v", "libx264", "-crf", "18", "-c:a", "aac", str(out_path)]
    return cmd, out_path, out_path


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
            "-c:v", "libx264", "-crf", "18", "-c:a", "aac",
            str(out_path),
        ]
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
            "-c:v", "libx264", "-crf", "18", "-c:a", "aac",
            str(out_path),
        ]
    return cmd, out_path, out_path


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
            else:
                dur_prev = ex._probe_clip_duration(clips[i-1])
                offset = max(0.0, dur_prev - t_dur)
                filter_parts.append(
                    f"{prev_v}[nv{i}]xfade=transition={t_type}:duration={t_dur}:offset={offset}{out_v};"
                    f"{prev_a}[na{i}]acrossfade=d={t_dur}{out_a}"
                )
            prev_v, prev_a = out_v, out_a
        cmd = [_ffmpeg_bin(), "-hide_banner", "-y"] + input_args
        cmd += ["-filter_complex", ";".join(filter_parts)]
        cmd += ["-map", "[vout]", "-map", "[aout]"]
        cmd += ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(out_path)]
    else:
        concat_list = ex._clips_dir / f"{output_name}_concat.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in clip_paths:
                f.write(f"file '{p}'\n")
        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(out_path),
        ]
    return cmd, out_path, out_path


def _build_mix_audio(args: dict, ex: EditPlanExecutor):
    video_clip = args.get("video_clip", "")
    audio_sources = args.get("audio_sources", [])
    volumes = args.get("volumes", [])
    fades = args.get("fades")
    normalization = bool(args.get("normalization", False))
    video_path = ex._resolve_clip(video_clip) or ex._clips_dir / f"{video_clip}.mp4"
    audio_paths = [ex._resolve_clip(a) for a in audio_sources]
    output_name = _sanitize_name(video_clip) + "_mixed"
    out_path = ex._clips_dir / f"{output_name}.mp4"

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

    cmd = [_ffmpeg_bin(), "-hide_banner", "-y", "-i", str(video_path)]
    for p in audio_paths:
        cmd += ["-i", str(p or audio_sources[0])]
    cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", "0:v", "-map", final_a_label]
    cmd += ["-c:v", "copy", "-c:a", "aac", str(out_path)]
    return cmd, out_path, out_path


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
    return cmd, out_path, out_path


def _build_validate(args: dict, ex: EditPlanExecutor):
    """Validate is a probe-only check — no ffmpeg transcode, just ffprobe."""
    target = args.get("target", "")
    kind = args.get("kind", "video")
    # Use ffprobe via a simple command; success = file is valid.
    from .video_production import _ffprobe_bin
    p = ex._resolve_clip(target) or ex._output_dir / target
    if p is None or not p.exists():
        p = ex._clips_dir / target
    cmd = [_ffprobe_bin(), "-hide_banner", "-show_format", "-show_streams", str(p)]
    # No output file for validate; checkpoint = None so it always runs.
    return cmd, None, None


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
        ffmpeg_cmd, _out, _ckpt = builder(cmd.args, ex)
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