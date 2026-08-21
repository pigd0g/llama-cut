"""Stage 8 — Final Video Production UI (chat-driven).

The user converses with an LLM agent via a chat interface to build an edit
plan from the storyboard. The plan is displayed as a Linear Edit Plan (LEP)
horizontal beat strip below the chat. Once approved via a confirmation
dialog, the user clicks "Run Edit Plan" and a deterministic executor runs
the queued ffmpeg commands with weighted progress and safe abort.

On a command failure, the failure details are fed back to the chat agent,
which proposes a fix via update_edit_plan. The user can then re-run with
checkpoint reuse (completed clips are skipped).

The storyboard is NOT shown on this screen — it's injected as context to
the agent.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..context import ContextStore
from .. import paths
from ..video_production import (
    EditPlan,
    VideoProductionSettings,
    clear_production,
    extract_beat_thumbnail,
    find_rendered_video,
    load_chat,
    load_edit_plan,
    save_chat,
)
from ..workers.chat_agent_worker import ChatAgentWorker, build_system_context
from ..workers.edit_executor_worker import EditExecutorWorker
from .chat_widget import ChatMessage, ChatWidget
from .debug_plan_dialog import DebugPlanDialog
from .edit_plan_widget import EditPlanWidget
from ..theme import (
    COLOR_ON_SURFACE_VARIANT,
    COLOR_DANGER,
    COLOR_SUCCESS,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
)

# Max consecutive auto-resumes after LLM-proposed fixes before requiring
# manual intervention. Prevents an infinite failure→fix→fail loop.
_MAX_AUTO_RESUMES = 30


class VideoProductionPage(QWidget):
    """Stage 8 — build an edit plan via chat, then run it."""

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._store: ContextStore | None = None
        self._chat_worker: ChatAgentWorker | None = None
        self._exec_worker: EditExecutorWorker | None = None
        self._system_context: str = ""
        self._edit_plan: EditPlan | None = None
        self._chat_messages: list[ChatMessage] = []
        self._raw_chat_messages: list[dict] = []
        # Auto-resume state: when the executor fails and the agent proposes a
        # fix via update_edit_plan, we automatically re-run the plan without
        # requiring the user to click "Run" again. Capped at _MAX_AUTO_RESUMES
        # consecutive attempts to avoid an infinite failure→fix→fail loop.
        self._auto_resume_pending: bool = False
        self._auto_resume_has_fix: bool = False
        self._auto_resume_count: int = 0
        self._build()

    # --- UI ----------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        # Header
        header = QHBoxLayout()
        title = QLabel("Final Video Production")
        title.setProperty("class", "headline-md")
        header.addWidget(title)
        header.addStretch()
        self.debug_btn = QPushButton("Debug")
        self.debug_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.debug_btn.clicked.connect(self._on_debug)
        header.addWidget(self.debug_btn)
        self.status_label = QLabel("")
        self.status_label.setProperty("class", "label-sm")
        header.addWidget(self.status_label)
        root.addLayout(header)

        # Chat widget (flexible height)
        model_name = self._get_model_name()
        self.chat_widget = ChatWidget(model_label=model_name)
        self.chat_widget.message_sent.connect(self._on_user_send)
        root.addWidget(self.chat_widget, 1)

        # LEP widget (fixed height horizontal strip)
        self.plan_widget = EditPlanWidget()
        self.plan_widget.beat_clicked.connect(self._on_beat_clicked)
        root.addWidget(self.plan_widget)

        # Progress bar (hidden until running)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        self.progress_label.setProperty("class", "label-sm")
        self.progress_label.setVisible(False)
        root.addWidget(self.progress_label)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch()
        self.back_btn = QPushButton("Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self._on_back)
        footer.addWidget(self.back_btn)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_btn.clicked.connect(self._on_reset)
        footer.addWidget(self.reset_btn)
        self.run_btn = QPushButton("Run Edit Plan")
        self.run_btn.setProperty("class", "primary")
        self.run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_btn.clicked.connect(self._on_run)
        footer.addWidget(self.run_btn)
        root.addLayout(footer)

    def _get_model_name(self) -> str:
        import os
        return os.environ.get("OLLAMA_WORKFLOW_MODEL", "")

    # --- Lifecycle ---------------------------------------------------------
    def on_enter(self) -> None:
        """Called when navigating to this stage."""
        if not self._state.working_folder:
            return
        self._store = ContextStore(paths.context_dir(self._state.working_folder))

        # Load existing edit plan
        self._edit_plan = load_edit_plan(self._state.working_folder)

        # Load chat history
        self._raw_chat_messages = load_chat(self._state.working_folder)
        self._chat_messages = [ChatMessage.from_dict(m) for m in self._raw_chat_messages]
        self.chat_widget.restore_from_messages(self._chat_messages)

        # Build the system context (storyboard + context + ffmpeg reference)
        # for the agent. This is built once on entry and reused.
        selected = self._state.selected_videos
        if selected:
            try:
                self._system_context = build_system_context(
                    self._state.working_folder, self._store, selected,
                )
            except Exception as e:
                self._system_context = ""
                self.status_label.setText(f"Context build error: {e}")
                self.status_label.setStyleSheet(f"color: {COLOR_DANGER};")

        # Update LEP
        self.plan_widget.update_plan(self._edit_plan)
        if self._edit_plan and self._edit_plan.timeline:
            self._extract_thumbnails()

        self._reset_button_states()

    def _extract_thumbnails(self) -> None:
        """Extract thumbnails for all beats in the current plan (async, deferred)."""
        if not self._edit_plan or not self._state.working_folder:
            return
        plan = self._edit_plan
        wf = self._state.working_folder
        wf_path = __import__("pathlib").Path(wf)

        def _do_extract():
            for beat in plan.timeline:
                # Skip if already cached
                if beat.thumbnail_path and __import__("os").path.exists(beat.thumbnail_path):
                    self.plan_widget.set_beat_thumbnail(beat.id, beat.thumbnail_path)
                    continue
                # Resolve the source filename to an absolute path in the
                # working folder (beat.source is just the filename, not a path).
                src_path = wf_path / beat.source
                if not src_path.exists():
                    continue
                thumb = extract_beat_thumbnail(
                    wf, str(src_path), beat.source_start, beat.source_end, beat.id,
                )
                if thumb:
                    beat.thumbnail_path = thumb
                    self.plan_widget.set_beat_thumbnail(beat.id, thumb)

        # Run in a deferred timer so the UI loads first
        QTimer.singleShot(100, _do_extract)

    # --- Chat ---------------------------------------------------------------
    def _on_user_send(self, text: str) -> None:
        """The user sent a message — start a chat agent turn."""
        if self._chat_worker is not None and self._chat_worker.isRunning():
            return
        if not self._state.working_folder or self._store is None:
            return

        # Show the user message immediately
        msg = self.chat_widget.add_user_message(text)
        self._chat_messages.append(msg)

        # Disable input while thinking
        self.chat_widget.set_input_enabled(False)
        self.run_btn.setEnabled(False)

        # Start the worker
        self._chat_worker = ChatAgentWorker(
            messages=self._raw_chat_messages,
            user_message=text,
            failure_context=None,
            working_folder=self._state.working_folder,
            selected_videos=self._state.selected_videos,
            context_store=self._store,
            system_context=self._system_context,
            parent=self,
        )
        self._chat_worker.thinking_started.connect(self._on_thinking_started)
        self._chat_worker.tool_called.connect(self._on_tool_called)
        self._chat_worker.assistant_text.connect(self._on_assistant_text)
        self._chat_worker.plan_updated.connect(self._on_plan_updated)
        self._chat_worker.thinking_ended.connect(self._on_thinking_ended)
        self._chat_worker.finished_error.connect(self._on_chat_error)
        self._chat_worker.start()

    def _on_thinking_started(self) -> None:
        self.chat_widget.show_thinking(sender=self._get_model_name())

    def _on_tool_called(self, tool_name: str, args_str: str,
                        result_str: str, success: bool, duration: float) -> None:
        msg = self.chat_widget.add_tool_chip(
            tool_name, args_str, result_str, success, duration,
        )
        self._chat_messages.append(msg)

    def _on_assistant_text(self, text: str) -> None:
        self.chat_widget.hide_thinking()
        msg = self.chat_widget.add_assistant_message(text, sender=self._get_model_name())
        self._chat_messages.append(msg)

    def _on_plan_updated(self, plan: EditPlan) -> None:
        self._edit_plan = plan
        self.plan_widget.update_plan(plan)
        if plan and plan.timeline:
            self._extract_thumbnails()
        # Track whether the agent produced a fix during a failure-recovery
        # turn. _feed_failure_to_agent sets _auto_resume_pending=True; if the
        # agent calls update_edit_plan during that turn, we auto-resume.
        if self._auto_resume_pending:
            self._auto_resume_has_fix = True
        self._reset_button_states()

    def _on_thinking_ended(self) -> None:
        self.chat_widget.hide_thinking()
        self.chat_widget.set_input_enabled(True)
        # Persist the raw chat messages from the worker
        if self._chat_worker is not None:
            self._raw_chat_messages = self._chat_worker.updated_messages
        self._reset_button_states()
        # Auto-resume: if this turn was a failure-recovery turn and the agent
        # proposed a fix (via update_edit_plan), re-run the edit plan
        # automatically without requiring the user to click "Run" again.
        if self._auto_resume_pending and self._auto_resume_has_fix:
            self._auto_resume_pending = False
            self._auto_resume_has_fix = False
            if self._auto_resume_count < _MAX_AUTO_RESUMES:
                self._auto_resume_count += 1
                self.status_label.setText(
                    f"Applying fix and resuming execution "
                    f"(attempt {self._auto_resume_count}/{_MAX_AUTO_RESUMES})…"
                )
                self.status_label.setStyleSheet(
                    f"color: {COLOR_ON_SURFACE_VARIANT};"
                )
                self._start_execution(self._edit_plan)
            else:
                self.status_label.setText(
                    f"Auto-resume limit reached ({_MAX_AUTO_RESUMES} attempts). "
                    f"Review the plan and click Run manually."
                )
                self.status_label.setStyleSheet(f"color: {COLOR_DANGER};")
                self._auto_resume_count = 0
        else:
            # Not a failure-recovery turn, or the agent failed to produce a
            # fix — clear the flags so a subsequent normal turn doesn't trip
            # the auto-resume path.
            self._auto_resume_pending = False
            self._auto_resume_has_fix = False

    def _on_chat_error(self, msg: str) -> None:
        self.chat_widget.hide_thinking()
        self.chat_widget.set_input_enabled(True)
        self.status_label.setText(f"Chat error: {msg}")
        self.status_label.setStyleSheet(f"color: {COLOR_DANGER};")
        # Clear auto-resume state — if the agent errored during a
        # failure-recovery turn, don't auto-resume; let the user review.
        self._auto_resume_pending = False
        self._auto_resume_has_fix = False
        self._reset_button_states()

    # --- Execution failure feedback to agent --------------------------------
    def _feed_failure_to_agent(self, failure_info: dict) -> None:
        """Inject an execution failure into the chat for the agent to fix."""
        if self._chat_worker is not None and self._chat_worker.isRunning():
            return
        if not self._state.working_folder or self._store is None:
            return

        # Mark this as a failure-recovery turn so _on_plan_updated and
        # _on_thinking_ended can auto-resume execution once the agent
        # proposes a fix via update_edit_plan.
        self._auto_resume_pending = True
        self._auto_resume_has_fix = False

        # Show a system message about the failure
        fail_msg = (
            f"⚠️ Execution failed at command '{failure_info.get('command_id')}' "
            f"({failure_info.get('command_type')}). I've asked the agent to "
            f"propose a fix."
        )
        msg = self.chat_widget.add_assistant_message(fail_msg, sender="system")
        self._chat_messages.append(msg)

        self.chat_widget.set_input_enabled(False)
        self.run_btn.setEnabled(False)

        self._chat_worker = ChatAgentWorker(
            messages=self._raw_chat_messages,
            user_message=None,
            failure_context=failure_info,
            working_folder=self._state.working_folder,
            selected_videos=self._state.selected_videos,
            context_store=self._store,
            system_context=self._system_context,
            parent=self,
        )
        self._chat_worker.thinking_started.connect(self._on_thinking_started)
        self._chat_worker.tool_called.connect(self._on_tool_called)
        self._chat_worker.assistant_text.connect(self._on_assistant_text)
        self._chat_worker.plan_updated.connect(self._on_plan_updated)
        self._chat_worker.thinking_ended.connect(self._on_thinking_ended)
        self._chat_worker.finished_error.connect(self._on_chat_error)
        self._chat_worker.start()

    # --- Run edit plan ------------------------------------------------------
    def _on_run(self) -> None:
        """Open a confirmation dialog, then start execution."""
        if self._exec_worker is not None and self._exec_worker.isRunning():
            # Currently running → abort
            self._exec_worker.cancel()
            return
        if self._edit_plan is None or not self._edit_plan.commands:
            self.status_label.setText("No commands to run. Build a plan first.")
            self.status_label.setStyleSheet(f"color: {COLOR_DANGER};")
            return
        if not self._state.working_folder:
            return

        # A manual run resets the auto-resume counter.
        self._auto_resume_count = 0

        plan = self._edit_plan
        beats = len(plan.timeline)
        cmds = len(plan.commands)
        duration = plan.target_duration or 0.0
        preset = plan.preset

        # Confirmation dialog
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Approve & Run Edit Plan")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText(
            f"Ready to execute the edit plan?\n\n"
            f"  Beats: {beats}\n"
            f"  Commands: {cmds}\n"
            f"  Target duration: {duration:.1f}s\n"
            f"  Preset: {preset}\n\n"
            f"This will run {cmds} ffmpeg operations sequentially. "
            f"You can abort safely at any time."
        )
        approve_btn = msg_box.addButton("Approve & Run", QMessageBox.ButtonRole.AcceptRole)
        msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        approve_btn.setStyleSheet(f"font-weight: 600;")
        if msg_box.exec() != QMessageBox.ButtonRole.AcceptRole.value and \
                msg_box.clickedButton() is not approve_btn:
            return

        self._start_execution(plan)

    def _start_execution(self, plan: EditPlan) -> None:
        """Mark the plan as approved and kick off the executor worker.

        Called from ``_on_run`` (after the confirmation dialog) and from the
        auto-resume path (after the agent proposes a fix). The auto-resume
        path skips the confirmation dialog because the user already approved
        the initial run.
        """
        plan.status = "approved"
        from ..video_production import save_edit_plan
        save_edit_plan(self._state.working_folder, plan)

        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress_bar.setValue(0)
        self.run_btn.setText("Abort")
        self.run_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_DANGER}; color: #ffffff; "
            f"border: 1px solid {COLOR_DANGER}; }}"
        )
        self.back_btn.setEnabled(False)
        self.reset_btn.setEnabled(False)
        self.debug_btn.setEnabled(False)

        self._exec_worker = EditExecutorWorker(
            working_folder=self._state.working_folder,
            plan=plan,
            parent=self,
        )
        self._exec_worker.progress.connect(self._on_exec_progress)
        self._exec_worker.log.connect(self._on_exec_log)
        self._exec_worker.finished_success.connect(self._on_exec_success)
        self._exec_worker.finished_error.connect(self._on_exec_error)
        self._exec_worker.start()

    def _on_exec_progress(self, p) -> None:
        pct = int(p.overall * 100)
        self.progress_bar.setValue(pct)
        self.progress_label.setText(
            f"{p.stage}: {p.message}  ({pct}%)"
        )

    def _on_exec_log(self, msg: str) -> None:
        # Could route to a debug log; for now, update the status label.
        pass

    def _on_exec_success(self, plan: EditPlan) -> None:
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.status_label.setText("Video production complete.")
        self.status_label.setStyleSheet(f"color: {COLOR_SUCCESS};")
        self._edit_plan = plan
        self._auto_resume_count = 0
        self._auto_resume_pending = False
        self._auto_resume_has_fix = False
        self._reset_button_states()
        # Auto-navigate to Result if a video was rendered
        if self._state.working_folder:
            if find_rendered_video(self._state.working_folder) is not None:
                self._state.set_stage(9)

    def _on_exec_error(self, msg: str, failure_info) -> None:
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.status_label.setText(f"Execution failed: {msg[:100]}")
        self.status_label.setStyleSheet(f"color: {COLOR_DANGER};")
        self._reset_button_states()
        # Feed the failure back to the chat agent for a fix proposal
        if failure_info is not None:
            self._feed_failure_to_agent(failure_info)

    # --- Debug --------------------------------------------------------------
    def _on_debug(self) -> None:
        dlg = DebugPlanDialog(self._edit_plan, self._state.working_folder, self)
        dlg.exec()

    # --- Beat click ---------------------------------------------------------
    def _on_beat_clicked(self, beat_id: str) -> None:
        """A beat was clicked — could focus it or show details. V1: no-op."""
        pass

    # --- Button states ------------------------------------------------------
    def _reset_button_states(self) -> None:
        has_plan = self._edit_plan is not None and bool(self._edit_plan.commands)
        is_executing = self._exec_worker is not None and self._exec_worker.isRunning()
        is_chatting = self._chat_worker is not None and self._chat_worker.isRunning()

        if is_executing:
            self.run_btn.setText("Abort")
            self.run_btn.setEnabled(True)
            self.run_btn.setStyleSheet(
                f"QPushButton {{ background-color: {COLOR_DANGER}; color: #ffffff; "
                f"border: 1px solid {COLOR_DANGER}; }}"
            )
            self.back_btn.setEnabled(False)
            self.reset_btn.setEnabled(False)
        else:
            self.run_btn.setText("Run Edit Plan")
            self.run_btn.setEnabled(has_plan and not is_chatting)
            self.run_btn.setStyleSheet("")  # reset to QSS default
            self.back_btn.setEnabled(True)
            self.reset_btn.setEnabled(has_plan and not is_chatting)
        self.debug_btn.setEnabled(self._edit_plan is not None)

    # --- Reset --------------------------------------------------------------
    def _on_reset(self) -> None:
        if self._exec_worker is not None and self._exec_worker.isRunning():
            return
        if self._chat_worker is not None and self._chat_worker.isRunning():
            return
        if not self._state.working_folder:
            return
        reply = QMessageBox.question(
            self, "Reset Production",
            "This will permanently delete all video production artefacts "
            "(chat, edit plan, clips, output). This cannot be undone.\n\n"
            "Are you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_production(self._state.working_folder)
        self._edit_plan = None
        self._chat_messages = []
        self._raw_chat_messages = []
        self.chat_widget.clear_messages()
        self.plan_widget.update_plan(None)
        self._state.set_video_production_settings(VideoProductionSettings(last_feedback=""))
        self.status_label.setText("Production reset.")
        self.status_label.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT};")
        self._reset_button_states()

    # --- Navigation --------------------------------------------------------
    def _on_back(self) -> None:
        self._state.set_stage(7)