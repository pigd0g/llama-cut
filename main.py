from __future__ import annotations

import sys

from dotenv import load_dotenv
from PyQt6.QtWidgets import QApplication

from src.app import AppShell
from src.state import PipelineState
from src.theme import DARK_QSS, register_fonts


def main() -> int:
    # override=True so values in .env take precedence over any ambient
    # process/user env vars (e.g. a locally-installed Ollama that sets
    # OLLAMA_HOST=0.0.0.0 in the system). The .env file is the source of truth.
    load_dotenv(override=True)
    app = QApplication(sys.argv)
    app.setApplicationName("llama-cut")
    register_fonts()
    app.setStyleSheet(DARK_QSS)
    state = PipelineState()
    window = AppShell(state)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())