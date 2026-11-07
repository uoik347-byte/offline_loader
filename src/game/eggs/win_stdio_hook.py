"""PyInstaller runtime hook: windowed builds start with no console handles."""
from __future__ import annotations

import sys


def ensure_stdio() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    import os
    from pathlib import Path

    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    path = base / "logs" / "egg-hatch-startup.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a", encoding="utf-8", errors="replace")
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


ensure_stdio()
