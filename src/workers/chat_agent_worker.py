"""ChatAgentWorker — runs one chat turn per user message in a QThread.

The worker is turn-based: each call to ``run_turn()`` (via ``start()`` with
the message pre-set) sends the current message transcript to the Ollama
model, executes any planning tools the agent calls (probe, inspect,
commit/update_edit_plan), and streams back the assistant's response.

Signals:
  - thinking_started(): emitted when a turn begins (before the first
      client.chat call). The chat widget shows the pulsing-dots indicator.
  - tool_called(str, str, str, bool, float): emitted per tool call.
      (tool_name, args_json, result_json, success, duration_s).
      The chat widget inserts an expandable tool chip.
  - assistant_text(str): emitted with the assistant's final text response.
      The chat widget replaces the thinking indicator with a message bubble.
  - plan_updated(EditPlan): emitted when commit_edit_plan or update_edit_plan
      is called, carrying the new plan. The page refreshes the LEP.
  - thinking_ended(): emitted when the turn completes (success or error).
  - finished_error(str): emitted on unexpected error.

The worker handles two entry types:
  1. Normal user message: the user's text is appended to the transcript.
  2. Execution failure feedback: a tool-role message with the failed
     command details is appended (no user text), triggering the agent to
     propose a fix via update_edit_plan.
"""
from __future__ import annotations

import json
import time
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from ..context import ContextStore
from ..context_review import load_assembled
from ..storyboard import build_context_markdown, load_latest_storyboard
from ..video_metadata import VideoMetadata, extract_metadata
from ..video_production import (
    EDITING_SYSTEM_PROMPT,
    ToolRegistry,
    build_chat_system_context,
    build_ollama_client,
    is_config_valid,
    load_ffmpeg_skill,
    load_video_production_config,
    save_chat,
    save_edit_plan,
)


class ChatAgentWorker(QThread):
    """Run a single chat turn (one user message or one failure feedback)."""

    thinking_started = pyqtSignal()
    tool_called = pyqtSignal(str, str, str, bool, float)
    assistant_text = pyqtSignal(str)
    plan_updated = pyqtSignal(object)  # EditPlan
    thinking_ended = pyqtSignal()
    finished_error = pyqtSignal(str)

    def __init__(
        self,
        messages: list[dict],
        user_message: str | None,
        failure_context: dict | None,
        working_folder: str,
        selected_videos: list,
        context_store: ContextStore,
        system_context: str,
        parent=None,
    ):
        super().__init__(parent)
        self._messages = list(messages)
        self._user_message = user_message
        self._failure_context = failure_context
        self._working_folder = working_folder
        self._selected_videos = list(selected_videos)
        self._context_store = context_store
        self._system_context = system_context
        self._cancel = False
        self._updated_messages: list[dict] = []

    @property
    def updated_messages(self) -> list[dict]:
        """The full message transcript after this turn (for persistence)."""
        return self._updated_messages

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:
            self.finished_error.emit(str(e))
        finally:
            self.thinking_ended.emit()

    def _run(self) -> None:
        config = load_video_production_config()
        ok, msg = is_config_valid(config)
        if not ok:
            self.finished_error.emit(msg)
            return

        try:
            client = build_ollama_client(config)
        except Exception as e:
            self.finished_error.emit(f"Failed to build Ollama client: {e}")
            return

        if self._cancel:
            return

        # Probe all selected videos (needed for plan validation).
        metadatas: list[VideoMetadata] = []
        for v in self._selected_videos:
            meta = extract_metadata(v.path)
            if meta is None:
                meta = VideoMetadata(source_filename=v.name, source_path=v.path)
            metadatas.append(meta)

        registry = ToolRegistry(
            working_folder=self._working_folder,
            selected_videos=self._selected_videos,
            metadatas=metadatas,
        )

        # Restore the current plan into the registry so plan amendments
        # build on the existing plan.
        from ..video_production import load_edit_plan
        existing = load_edit_plan(self._working_folder)
        if existing is not None:
            registry._current_plan = existing

        # Build the message transcript for this turn.
        messages: list[dict] = [
            {"role": "system", "content": EDITING_SYSTEM_PROMPT},
        ]
        # The system context (storyboard + context + ffmpeg reference) is
        # the first user message — included once at the start of the
        # transcript. If the existing messages already contain it (restored
        # from chat.json), don't duplicate.
        has_context = any(
            m.get("role") == "user" and "## Storyboard" in (m.get("content") or "")
            for m in self._messages
        )
        if not has_context:
            messages.append({"role": "user", "content": self._system_context})
            messages.append({
                "role": "assistant",
                "content": (
                    "I've reviewed your storyboard and the available context. "
                    "I'm ready to help you build an edit plan. What kind of "
                    "video are you aiming for?"
                ),
            })
        messages.extend(self._messages)

        # Append the new user message or failure context.
        if self._user_message:
            messages.append({"role": "user", "content": self._user_message})
        elif self._failure_context:
            messages.append({
                "role": "user",
                "content": (
                    "## Execution Failure\n\n"
                    f"The edit plan execution failed at command "
                    f"'{self._failure_context.get('command_id')}' "
                    f"({self._failure_context.get('command_type')}).\n\n"
                    f"Error: {self._failure_context.get('error')}\n\n"
                    f"ffmpeg stderr:\n```\n{self._failure_context.get('stderr', '')[:1500]}\n```\n\n"
                    f"Command args: "
                    f"{json.dumps(self._failure_context.get('args', {}))}\n\n"
                    f"Analyse the error and propose a fix by calling "
                    f"update_edit_plan() with the corrected plan. Explain "
                    f"the fix to me in plain English."
                ),
            })

        self._updated_messages = list(messages)
        self.thinking_started.emit()

        tools = registry.get_chat_tools()
        all_callables = registry.get_tools()

        max_rounds = 30
        for round_trip in range(max_rounds):
            if self._cancel:
                return

            response = client.chat(
                model=config.model,
                messages=messages,
                tools=tools,
            )
            assistant_msg = response.message
            messages.append(assistant_msg)

            tool_calls = getattr(assistant_msg, "tool_calls", None)
            if not tool_calls:
                final_text = getattr(assistant_msg, "content", "") or ""
                self.assistant_text.emit(final_text)
                self._updated_messages = messages
                # Persist the chat transcript (exclude the system message
                # and the large context message to keep the file small).
                self._persist_chat(messages)
                return

            # Execute each tool call.
            for call in tool_calls:
                tool_name = call.function.name
                args = call.function.arguments
                args_str = json.dumps(args) if isinstance(args, dict) else str(args)

                round_start = time.time()
                result_data = _execute_tool_call(all_callables, tool_name, args)
                round_dur = time.time() - round_start

                success = not bool(result_data.get("error"))
                result_str = json.dumps(result_data)

                self.tool_called.emit(tool_name, args_str, result_str, success, round_dur)

                # Detect plan commit/update.
                if tool_name in ("commit_edit_plan", "update_edit_plan") \
                        and registry.current_plan is not None:
                    self.plan_updated.emit(registry.current_plan)

                messages.append({
                    "role": "tool",
                    "content": result_str,
                })

        # Round budget exhausted — emit whatever text we have.
        self.assistant_text.emit(
            "I've been working on this for a while. Could you clarify what "
            "you'd like me to adjust?"
        )
        self._updated_messages = messages
        self._persist_chat(messages)

    def _persist_chat(self, messages: list[dict]) -> None:
        """Persist the chat transcript, stripping the large context message."""
        to_save: list[dict] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            # Skip the system prompt (always re-injected) and the large
            # context message (re-injected from disk on load).
            if role == "system":
                continue
            if role == "user" and "## Storyboard" in (content or ""):
                continue
            # Serialize tool-call assistant messages carefully.
            if role == "assistant" and hasattr(m, "model_dump"):
                try:
                    d = m.model_dump()
                    to_save.append(d)
                    continue
                except Exception:
                    pass
            if isinstance(m, dict):
                to_save.append(m)
        save_chat(self._working_folder, to_save)


def _execute_tool_call(tools: list, tool_name: str, args: Any) -> dict:
    """Execute a tool call by dispatching to the matching function."""
    tool_func = None
    for t in tools:
        if getattr(t, "__name__", "") == tool_name:
            tool_func = t
            break
    if tool_func is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        if isinstance(args, dict):
            result = tool_func(**args)
        else:
            result = tool_func(args)
        # Unwrap a to_tool_message() dict back to the inner data dict.
        if isinstance(result, dict) and result.get("role") == "tool" \
                and "content" in result and "error" not in result:
            try:
                return json.loads(result["content"])
            except (json.JSONDecodeError, TypeError):
                return {"error": f"Tool returned unreadable result"}
        if isinstance(result, dict):
            return result
        return {"result": str(result)}
    except Exception as e:
        return {"error": f"Tool execution error: {e}"}


def build_system_context(working_folder: str, context_store: ContextStore,
                         selected_videos: list) -> str:
    """Build the system context message (storyboard + context + ffmpeg ref).

    Called by the page on entry to Stage 8 so the context is ready before
    the first chat turn.
    """
    storyboard_md = load_latest_storyboard(working_folder)
    doc = _load_assembled(context_store, selected_videos)
    metadatas: list[VideoMetadata] = []
    for v in selected_videos:
        meta = extract_metadata(v.path)
        if meta is None:
            meta = VideoMetadata(source_filename=v.name, source_path=v.path)
        metadatas.append(meta)
    context_md = build_context_markdown(
        project_ctx=doc.project_context,
        video_sections=doc.videos,
        video_metadatas=metadatas,
        working_folder=working_folder,
    )
    ffmpeg_skill = load_ffmpeg_skill()
    return build_chat_system_context(storyboard_md, context_md, ffmpeg_skill)


def _load_assembled(context_store: ContextStore, selected_videos: list):
    """Load the assembled context document from the ContextStore."""
    from types import SimpleNamespace
    state = SimpleNamespace(selected_videos=selected_videos)
    return load_assembled(state, context_store)