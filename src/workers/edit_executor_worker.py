"""EditExecutorWorker — runs EditPlanExecutor in a background QThread.

Signals:
  - progress(ExecutorProgress): cumulative weighted progress.
  - log(str): detailed log line.
  - command_started(dict): a command started running.
  - command_finished(dict, bool): a command finished (dict = result summary,
      bool = success).
  - finished_success(EditPlan): all commands completed; the plan with
      updated output_path/status.
  - finished_error(str, dict | None): execution failed or was aborted.
      str = human message; dict | None = failed command details for LLM
      feedback (None on cancel).
"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from ..edit_plan_executor import (
    CommandResult,
    EditPlanExecutor,
    ExecutorProgress,
)
from ..video_production import (
    EditPlan,
    find_rendered_video,
    save_edit_plan,
)


class EditExecutorWorker(QThread):
    """Run the edit plan's ffmpeg commands in a background thread."""

    progress = pyqtSignal(object)   # ExecutorProgress
    log = pyqtSignal(str)
    command_started = pyqtSignal(dict)
    command_finished = pyqtSignal(dict, bool)
    finished_success = pyqtSignal(object)  # EditPlan
    finished_error = pyqtSignal(str, object)  # str, dict|None

    def __init__(self, working_folder: str, plan: EditPlan, parent=None):
        super().__init__(parent)
        self._working_folder = working_folder
        self._plan = plan
        self._executor: EditPlanExecutor | None = None
        self._cancel = False

    def cancel(self) -> None:
        """Abort the current command and halt the run."""
        self._cancel = True
        if self._executor is not None:
            self._executor.cancel()

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            self.log.emit(f"Unexpected error: {e}")
            self.finished_error.emit(str(e), None)

    def _run(self) -> None:
        self._plan.status = "executing"
        save_edit_plan(self._working_folder, self._plan)

        self._executor = EditPlanExecutor(
            working_folder=self._working_folder,
            plan=self._plan,
            progress_cb=self._on_progress,
            log_cb=lambda msg: self.log.emit(msg),
            is_cancelled=lambda: self._cancel,
        )

        success, failed_result, all_results = self._executor.run()

        if self._cancel:
            self._plan.status = "draft"
            save_edit_plan(self._working_folder, self._plan)
            self.finished_error.emit("Aborted by user.", None)
            return

        if not success and failed_result is not None:
            self._plan.status = "failed"
            # Record the failed command in the plan.
            for cmd in self._plan.commands:
                if cmd.id == failed_result.command.id:
                    cmd.status = "failed"
                    cmd.error = failed_result.error
                    break
            save_edit_plan(self._working_folder, self._plan)
            # Build a failure report dict for the chat agent.
            failure_info = {
                "command_id": failed_result.command.id,
                "command_type": failed_result.command.type,
                "beat_id": failed_result.command.beat_id,
                "args": failed_result.command.args,
                "error": failed_result.error,
                "stderr": failed_result.stderr,
            }
            self.finished_error.emit(
                f"Command '{failed_result.command.id}' ({failed_result.command.type}) failed: "
                f"{failed_result.error}",
                failure_info,
            )
            return

        # Success — update the plan with output_path + status.
        rendered = find_rendered_video(self._working_folder)
        if rendered is not None:
            self._plan.output_path = str(rendered)
        self._plan.status = "rendered"
        # Mark all commands as done.
        for cmd in self._plan.commands:
            if cmd.status == "pending":
                cmd.status = "done"
        save_edit_plan(self._working_folder, self._plan)
        self.log.emit("Edit plan execution complete.")
        self.finished_success.emit(self._plan)

    def _on_progress(self, p: ExecutorProgress) -> None:
        self.progress.emit(p)