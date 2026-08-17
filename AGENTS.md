# Project Guidelines

## Overview

`llama-cut` is a Python desktop GUI application built with **PyQt6**. It implements a
video frame extraction and metadata generation pipeline. All Python work runs inside a
local virtual environment (`.venv`).

## Build and Test

Install the full dependency set from `requirements.txt`:

```
python -m pip install -r requirements.txt
```

This project is not a library — install with `pip`, not a package manager.

## Dependencies

- `requirements.txt` is the single source of truth for dependencies.
- **Always** invoke pip as `python -m pip` (venv-safe), never bare `pip`.
- **Always** run `python -m pip freeze > requirements.txt` after installing or upgrading
  any package, so the lock file stays in sync with the venv.
- Never hand-edit `requirements.txt`; regenerate it with `python -m pip freeze`.

## UI (PyQt6)

- All UI code **must** use **PyQt6**. Do not use Tkinter, PySide, or other GUI toolkits.
- When working on a UI design request, load and follow the **`pyqt6-ui-designer`** skill.
- Follow the PyQt6 development rules from the **`pyqt6-ui-development-rules`** skill
  (signal/slot architecture, QSS theming, QThread concurrency, layout management,
  cross-platform rendering, MVC separation).

## Skill Usage

- Use **`pyqt6-ui-designer`** for any UI styling / design work.
- Use **`pyqt6-ui-development-rules`** as the authoritative rule set for PyQt6 code.
- Use **`python-ffmpeg`** skill for information on using ffmpeg with python.