"""Presentation checks for the end-user window. No live login."""
from __future__ import annotations

import unittest
from pathlib import Path

from src.game.eggs.engine import QueryView
from src.game.eggs.protocol import PendingEgg
from src.game.eggs.store import HoldStore
from src.game.eggs.user_text import progress_text, run_status, slot_text, visible_failure

ROOT = Path(__file__).resolve().parents[1]
_BANNED = ("eggId", "pmid", "1-8", "1=1", "1-4", "1223", "2223", "ext=", "cmd=", "Traceback")


class UserCopyTests(unittest.TestCase):
    def test_unknown_slots_are_not_called_empty(self) -> None:
        text = slot_text(HoldStore(), fresh=False, pending=None)
        self.assertIn("位置 1：未知", text)
        self.assertIn("位置 2：未知", text)
        self.assertIn("位置 3：未知", text)
        self.assertNotIn("空着", text)
        self.assertIn("一次只用一个空位", text)
        for marker in _BANNED:
            self.assertNotIn(marker, text)

    def test_absent_unconfirmed_is_not_a_success_count(self) -> None:
        text = run_status("idle", 2, 1)
        self.assertIn("已确认收取 2", text)
        self.assertIn("回执未确认 1", text)
        self.assertIn("不计为成功", text)
        progress = progress_text(2, 1, QueryView(True, "ok", "", [], [9, 8]))
        self.assertIn("上次查询还有 2 个", progress)
        self.assertIn("不是实时数量", progress)

    def test_live_unknown_slot_is_not_assigned(self) -> None:
        store = HoldStore()
        pending = [PendingEgg(4, 40, 100)]
        text = slot_text(store, fresh=True, pending=pending)
        self.assertIn("位置 1：空着", text)
        self.assertIn("不知道在哪个位置", text)
        self.assertNotIn("位置 1：已经孵好", text)

    def test_failure_text_hides_protocol_fields(self) -> None:
        text = visible_failure(RuntimeError("eggId=14523 pmid=32750 cmd=1223"))
        self.assertNotIn("14523", text)
        self.assertNotIn("32750", text)
        self.assertNotIn("1223", text)
        self.assertIn("未知", text)


class UserWindowTests(unittest.TestCase):
    def test_window_has_actions_and_no_technical_log(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            self.skipTest(f"window toolkit unavailable: {exc}")
        root = tk.Tk()
        root.withdraw()
        from src.game.eggs.user_ui import UserApp

        app = UserApp(root, store_path=ROOT / "logs" / "user-ui-state.json")
        try:
            root.deiconify()
            root.update_idletasks()
            root.update()
            names = []
            for child in _walk(root):
                try:
                    names.append(str(child.cget("text")))
                except Exception:
                    continue
            labels = " ".join(names)
            self.assertIn("开始", labels)
            self.assertIn("停止", labels)
            self.assertIn("收取", labels)
            self.assertIn("查看孵化器", labels)
            self.assertNotIn("复制日志", labels)
            self.assertNotIn("保存日志", labels)
            self.assertNotIn("确认格子已空", labels)
            shown = app.slots.get("1.0", "end") + app.progress.get("1.0", "end") + app.status.get()
            for marker in _BANNED:
                self.assertNotIn(marker, shown)
            self.assertGreater(max(app.slots.winfo_height(), app.slots.winfo_reqheight()), 40)
            self.assertFalse(hasattr(app, "log_box"))
            app.engine = _FakeEngine()
            app._on_stop()
            self.assertTrue(app.engine.stopped)
        finally:
            app._on_close()


class _FakeEngine:
    def __init__(self) -> None:
        self.stopped = False
        self.collected_ids: list[int] = []
        self.absent_unconfirmed: list[int] = []
        self._fresh = False
        self._fresh_pending = []
        self.abort_requested = False

    def request_stop(self) -> None:
        self.stopped = True

    def request_abort(self) -> None:
        return None

    def begin(self) -> None:
        return None


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)
