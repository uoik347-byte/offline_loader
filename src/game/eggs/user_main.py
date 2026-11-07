"""End-user window entry. Startup failures stay in a short dialog."""
from __future__ import annotations

import sys

from src.game.eggs.win_stdio_hook import ensure_stdio

ensure_stdio()


def _show_startup_error() -> None:
    text = "程序没有打开。请重新打开再试。"
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("亚比蛋孵化", text)
        root.destroy()
    except Exception:
        sys.stderr.write(text + "\n")


def main() -> None:
    try:
        from src.game.eggs.user_ui import main as run_ui

        run_ui()
    except Exception:
        _show_startup_error()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
