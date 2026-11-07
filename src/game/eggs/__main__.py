"""Windowed entry. Startup failures stay in the GUI or the redacted log."""
from __future__ import annotations

import sys

from src.game.eggs.win_stdio_hook import ensure_stdio

ensure_stdio()


def _startup_text(exc: BaseException) -> str:
    from src.game.eggs.protocol import public_error

    return "程序启动失败。\n" + public_error(exc)


def _show_startup_error(exc: BaseException) -> None:
    text = _startup_text(exc)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("亚比蛋孵化收取", text)
        root.destroy()
    except Exception:
        sys.stderr.write(text + "\n")


def main() -> None:
    try:
        from src.game.eggs.ui import main as run_ui

        run_ui()
    except Exception as exc:
        _show_startup_error(exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
