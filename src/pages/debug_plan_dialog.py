"""DebugPlanDialog — modal showing the raw edit plan JSON + queued ffmpeg commands.

Opened from a "Debug" button in the Stage 8 page header. Shows:
  - Raw Edit Plan JSON (syntax-highlighted via a read-only plain text edit).
  - Queued ffmpeg commands (each rendered as a copyable command-line string,
    listed in execution order with beat linkage).

Read-only — no editing. For inspection/debugging only.
"""
from __future__ import annotations

import json

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..edit_plan_executor import EditPlanExecutor, render_command_as_string
from ..video_production import load_tool_log, load_tool_log_meta
from ..theme import (
    COLOR_BORDER,
    COLOR_ON_SURFACE,
    COLOR_ON_SURFACE_VARIANT,
    COLOR_SURFACE,
    COLOR_SURFACE_CONTAINER,
    RADIUS_LG,
    RADIUS_MD,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XS,
)


class DebugPlanDialog(QDialog):
    """Modal dialog showing the raw edit plan + queued ffmpeg commands."""

    def __init__(self, plan, working_folder: str = "", parent=None):
        super().__init__(parent)
        self._plan = plan
        self._working_folder = working_folder
        self.setWindowTitle("Debug — Edit Plan & FFmpeg Commands")
        self.setModal(True)
        self.resize(900, 600)
        self.setStyleSheet(f"""
            QDialog {{ background-color: {COLOR_SURFACE}; }}
        """)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING_LG, SPACING_LG, SPACING_LG, SPACING_LG)
        root.setSpacing(SPACING_MD)

        # Tab widget: Plan JSON | FFmpeg Commands
        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_LG}px;
                background-color: {COLOR_SURFACE_CONTAINER};
            }}
            QTabBar::tab {{
                background: transparent;
                color: {COLOR_ON_SURFACE_VARIANT};
                padding: {SPACING_SM}px {SPACING_MD}px;
                border: 1px solid transparent;
            }}
            QTabBar::tab:selected {{
                background-color: {COLOR_SURFACE_CONTAINER};
                color: {COLOR_ON_SURFACE};
                border-color: {COLOR_BORDER};
            }}
        """)

        # Tab 1: Raw plan JSON
        json_tab = QWidget()
        json_lay = QVBoxLayout(json_tab)
        json_lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        json_edit = QPlainTextEdit()
        json_edit.setReadOnly(True)
        mono = QFont("Cascadia Code", 10)
        if not mono.exactMatch():
            mono = QFont("Consolas", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        json_edit.setFont(mono)
        json_edit.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {COLOR_SURFACE};
                color: {COLOR_ON_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MD}px;
                padding: {SPACING_SM}px;
            }}
        """)
        if self._plan is not None:
            plan_json = self._plan.model_dump_json(indent=2)
        else:
            plan_json = "{}"
        json_edit.setPlainText(plan_json)
        json_lay.addWidget(json_edit)

        # Copy button
        copy_json_btn = QPushButton("Copy JSON")
        copy_json_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_json_btn.clicked.connect(lambda: QApplication.clipboard().setText(plan_json))
        json_lay.addWidget(copy_json_btn)
        tabs.addTab(json_tab, "Plan JSON")

        # Tab 2: FFmpeg commands
        cmds_tab = QWidget()
        cmds_lay = QVBoxLayout(cmds_tab)
        cmds_lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        cmds_lay.setSpacing(SPACING_SM)

        if self._plan is not None and self._plan.commands:
            # Build a lightweight executor just for rendering commands.
            ex = EditPlanExecutor(self._working_folder, self._plan) if self._working_folder else None

            # Scrollable container so a long command list doesn't overflow.
            cmds_scroll = QScrollArea()
            cmds_scroll.setWidgetResizable(True)
            cmds_scroll.setFrameShape(QFrame.Shape.NoFrame)
            cmds_scroll.setStyleSheet(f"""
                QScrollArea {{ background-color: transparent; border: none; }}
                QScrollBar:vertical {{
                    width: 8px; background: transparent;
                }}
                QScrollBar::handle:vertical {{
                    background: {COLOR_BORDER}; border-radius: 4px; min-height: 32px;
                }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
                QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                    background: none; height: 0; border: none;
                }}
            """)

            cmds_content = QWidget()
            cmds_content.setStyleSheet("background-color: transparent;")
            cmds_inner = QVBoxLayout(cmds_content)
            cmds_inner.setContentsMargins(0, 0, 0, 0)
            cmds_inner.setSpacing(SPACING_SM)

            for i, cmd in enumerate(self._plan.commands):
                cmd_frame = QPlainTextEdit()
                cmd_frame.setReadOnly(True)
                cmd_frame.setFont(mono)
                cmd_frame.setStyleSheet(f"""
                    QPlainTextEdit {{
                        background-color: {COLOR_SURFACE};
                        color: {COLOR_ON_SURFACE};
                        border: 1px solid {COLOR_BORDER};
                        border-radius: {RADIUS_MD}px;
                        padding: {SPACING_SM}px;
                    }}
                """)
                beat_link = f" [beat: {cmd.beat_id}]" if cmd.beat_id else ""
                cmd_str = render_command_as_string(cmd, ex)
                cmd_frame.setPlainText(
                    f"# {i+1}. {cmd.id} ({cmd.type}){beat_link} — status: {cmd.status}\n"
                    f"{cmd_str}"
                )
                # Height adapts to the command length so long filter graphs
                # are fully visible without intra-widget scrolling, while the
                # outer scroll area handles the overall list length.
                line_count = max(2, cmd_frame.blockCount())
                line_h = int(cmd_frame.fontMetrics().lineSpacing())
                h = min(400, line_count * line_h + 2 * SPACING_SM + 6)
                cmd_frame.setMinimumHeight(h)
                cmd_frame.setMaximumHeight(h)
                cmds_inner.addWidget(cmd_frame)

            cmds_inner.addStretch()
            cmds_scroll.setWidget(cmds_content)
            cmds_lay.addWidget(cmds_scroll, 1)
        else:
            empty = QLabel("No commands in the plan.")
            empty.setStyleSheet(f"color: {COLOR_ON_SURFACE_VARIANT}; padding: {SPACING_LG}px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cmds_lay.addWidget(empty)

        tabs.addTab(cmds_tab, f"FFmpeg Commands ({len(self._plan.commands) if self._plan else 0})")

        # Tab 3: Execution Log (commands that actually ran + their output/errors)
        log_tab = QWidget()
        log_lay = QVBoxLayout(log_tab)
        log_lay.setContentsMargins(SPACING_MD, SPACING_MD, SPACING_MD, SPACING_MD)
        log_lay.setSpacing(SPACING_SM)

        exec_log = load_tool_log(self._working_folder) if self._working_folder else []
        log_meta = load_tool_log_meta(self._working_folder) if self._working_folder else {}

        log_edit = QPlainTextEdit()
        log_edit.setReadOnly(True)
        log_edit.setFont(mono)
        log_edit.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {COLOR_SURFACE};
                color: {COLOR_ON_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MD}px;
                padding: {SPACING_SM}px;
            }}
        """)
        if exec_log:
            lines: list[str] = []
            # Summary header: plan status + per-status counts.
            if log_meta:
                lines.append("=" * 78)
                lines.append(
                    f"EXECUTION SUMMARY — plan status: {log_meta.get('plan_status', '?')}"
                    f"  ({log_meta.get('timestamp', '')})"
                )
                lines.append(
                    f"  total: {log_meta.get('total_commands', len(exec_log))}  "
                    f"ran: {log_meta.get('ran', '?')}  "
                    f"done: {log_meta.get('succeeded', '?')}  "
                    f"skipped: {log_meta.get('skipped', '?')}  "
                    f"failed: {log_meta.get('failed', '?')}  "
                    f"not run: {log_meta.get('not_run', '?')}"
                )
                lines.append("=" * 78)
            for entry in exec_log:
                status = entry.get("status", "?")
                marker = {
                    "done": "[OK]",
                    "skipped": "[SKIP]",
                    "failed": "[FAIL]",
                    "not_run": "[NOT RUN]",
                }.get(status, f"[{status.upper()}]")
                lines.append(f"{'=' * 78}")
                lines.append(
                    f"{marker} {entry.get('id', '?')} ({entry.get('type', '?')}) — {status}"
                    f"  ({entry.get('duration_s', 0)}s)"
                )
                beat = entry.get("beat_id")
                if beat:
                    lines.append(f"  beat: {beat}")
                lines.append(f"  command: {entry.get('command', '')}")
                if entry.get("output_path"):
                    lines.append(f"  output: {entry.get('output_path', '')}")
                if entry.get("error"):
                    lines.append(f"  ERROR: {entry.get('error', '')}")
                if entry.get("stderr"):
                    lines.append("  stderr:")
                    for sl in entry.get("stderr", "").splitlines():
                        lines.append(f"    {sl}")
                lines.append("")
            log_edit.setPlainText("\n".join(lines))
        else:
            log_edit.setPlainText("No commands have been executed yet. Run the edit plan to populate this log.")
        log_lay.addWidget(log_edit)

        copy_log_btn = QPushButton("Copy Log")
        copy_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _log_text = log_edit.toPlainText()
        copy_log_btn.clicked.connect(lambda: QApplication.clipboard().setText(_log_text))
        log_lay.addWidget(copy_log_btn)
        tabs.addTab(log_tab, f"Execution Log ({len(exec_log)})")

        root.addWidget(tabs, 1)

        # Close button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setProperty("class", "primary")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)