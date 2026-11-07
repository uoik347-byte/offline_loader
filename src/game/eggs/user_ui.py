"""End-user window. Same engine as the verified build; no technical log."""
from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from src.config import PROJECT_ROOT
from src.game.eggs.engine import EggEngine
from src.game.eggs.session import CancelledStep, EggSession, format_zone_choice
from src.game.eggs.store import JsonHoldStore
from src.game.eggs.transport import ContextTransport
from src.game.eggs.user_text import (
    collect_confirmation,
    progress_text,
    run_status,
    slot_text,
    start_confirmation,
    visible_failure,
)


class UserApp:
    def __init__(self, root: tk.Tk, store_path: Path | None = None) -> None:
        self.root = root
        self.root.title("亚比蛋孵化")
        self.root.geometry("860x640")
        self.root.minsize(720, 560)
        self.session = EggSession()
        self.store = JsonHoldStore(store_path or (PROJECT_ROOT / "egg-hatch-local-state.json"))
        self._roles: list[tuple[int, str]] = []
        self._zones: list[tuple[int, str]] = []
        self._busy = False
        self._op = 0
        self._future = None
        self.engine: EggEngine | None = None
        self.query = None
        self.running = False
        self._seen_unconfirmed = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._build()
        self._refresh_view()
        self._set_status("已打开，尚未开始。请先查询角色。")
        self.root.after(500, self._watch_progress)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _build(self) -> None:
        self.root.option_add("*Font", ("Microsoft YaHei UI", 11))
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        login = ttk.LabelFrame(frame, text="登录", padding=10)
        login.pack(fill=tk.X)
        self.account = tk.StringVar()
        self.password = tk.StringVar()
        self.role = tk.StringVar()
        self.zone = tk.StringVar()
        self.status = tk.StringVar()
        _field(login, 0, "账号", self.account, secret=False)
        _field(login, 1, "密码", self.password, secret=True)
        ttk.Label(login, text="角色").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.role_box = ttk.Combobox(login, textvariable=self.role, state="readonly", width=42)
        self.role_box.grid(row=2, column=1, sticky=tk.W)
        ttk.Label(login, text="区服").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.zone_box = ttk.Combobox(login, textvariable=self.zone, state="readonly", width=42)
        self.zone_box.grid(row=3, column=1, sticky=tk.W)
        buttons = ttk.Frame(login)
        buttons.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=8)
        self.lookup_button = ttk.Button(buttons, text="查询角色", command=self._on_lookup)
        self.auth_button = ttk.Button(buttons, text="验证登录", command=self._on_auth)
        self.connect_button = ttk.Button(buttons, text="连接区服", command=self._on_connect)
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._on_cancel)
        self.disconnect_button = ttk.Button(buttons, text="断开", command=self._on_disconnect)
        for button in (
            self.lookup_button,
            self.auth_button,
            self.connect_button,
            self.cancel_button,
            self.disconnect_button,
        ):
            button.pack(side=tk.LEFT, padx=(0, 8))

        work = ttk.LabelFrame(frame, text="孵化", padding=10)
        work.pack(fill=tk.BOTH, expand=True, pady=12)
        actions = ttk.Frame(work)
        actions.pack(fill=tk.X)
        self.query_button = ttk.Button(actions, text="查看孵化器", command=self._on_query)
        self.start_button = ttk.Button(actions, text="开始", command=self._on_start)
        self.stop_button = ttk.Button(actions, text="停止", command=self._on_stop)
        self.collect_button = ttk.Button(actions, text="收取", command=self._on_collect)
        for button in (self.query_button, self.start_button, self.stop_button, self.collect_button):
            button.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(work, textvariable=self.status, wraplength=760, justify=tk.LEFT).pack(
            anchor=tk.W, pady=8, fill=tk.X
        )
        self.slots = tk.Text(work, height=8, wrap=tk.WORD)
        self.slots.pack(fill=tk.X, pady=4)
        self.progress = tk.Text(work, height=4, wrap=tk.WORD)
        self.progress.pack(fill=tk.X, pady=4)
        for widget in (self.slots, self.progress):
            widget.configure(state=tk.DISABLED)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_status(self, text: str) -> None:
        self.status.set(text)

    def _refresh_view(self) -> None:
        connected = self.session.connected
        verified = self.session.password_verified
        self.lookup_button.configure(state=_enabled(not self._busy and not self.running))
        self.auth_button.configure(state=_enabled(bool(self._roles) and not self._busy and not self.running))
        self.connect_button.configure(
            state=_enabled(verified and bool(self._zones) and not self._busy and not self.running)
        )
        self.cancel_button.configure(state=_enabled(self._busy and not self.running))
        self.query_button.configure(state=_enabled(connected and not self.running and not self._busy))
        can_start = bool(
            connected and self.store.ok and self.query is not None and self.query.ok and not self.running and not self._busy
        )
        self.start_button.configure(state=_enabled(can_start))
        self.stop_button.configure(state=_enabled(self.running))
        self.collect_button.configure(state=_enabled(connected and not self.running and not self._busy))
        self.disconnect_button.configure(state=_enabled((connected or self.running) and not self._busy))
        fresh = bool(self.engine is not None and self.engine._fresh)
        pending = self.engine._fresh_pending if fresh else None
        _set_text(self.slots, slot_text(self.store, fresh=fresh, pending=pending))
        confirmed = len(self.engine.collected_ids) if self.engine is not None else 0
        unconfirmed = len(self.engine.absent_unconfirmed) if self.engine is not None else 0
        _set_text(self.progress, progress_text(confirmed, unconfirmed, self.query))

    def _watch_progress(self) -> None:
        if self.engine is not None and self.running:
            unconfirmed = len(self.engine.absent_unconfirmed)
            if unconfirmed > self._seen_unconfirmed:
                self._seen_unconfirmed = unconfirmed
                self._set_status("回执未确认，蛋已不在孵化器；继续运行。这一次不计为成功。")
            self._refresh_view()
        self.root.after(500, self._watch_progress)

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _silent(self, _message: str) -> None:
        return None

    def _on_lookup(self) -> None:
        account = self.account.get().strip()
        if not account:
            self._set_status("请先填写账号。")
            return
        self.session.cancel_requested = False
        self._busy = True
        self._set_status("正在查询角色。")
        self._refresh_view()

        async def action():
            return await self.session.lookup_roles(account, self._silent)

        self._submit(action(), self._lookup_done, "没有查到角色。")

    def _lookup_done(self, roles) -> None:
        self._roles = [(role.char_id, role.nickname) for role in roles]
        labels = [f"{nickname}（{char_id}）" for char_id, nickname in self._roles]
        self.role_box.configure(values=labels)
        if len(self._roles) == 1:
            self.role.set(labels[0])
            self._set_status("已找到角色。请输入密码后再验证。")
        else:
            self.role.set("")
            self._set_status(f"找到 {len(self._roles)} 个角色。请选择一个，再输入密码。")
        self._zones = []
        self.zone_box.configure(values=[])
        self.zone.set("")
        self.query = None
        self._refresh_view()

    def _on_auth(self) -> None:
        char_id = _selected_role(self.role.get(), self._roles)
        if char_id is None:
            self._set_status("请先选择角色。")
            return
        account = self.account.get().strip()
        password = self.password.get()
        self.password.set("")
        if not password:
            self._set_status("请输入密码。")
            return
        self.session.cancel_requested = False
        self._busy = True
        self._set_status("正在验证登录。")
        self._refresh_view()

        async def action():
            return await self.session.authenticate(account, password, char_id, self._silent)

        self._submit(action(), self._auth_done, "登录没有通过。")

    def _auth_done(self, zones) -> None:
        self._zones = [(zone.zone_index, format_zone_choice(zone)) for zone in zones]
        labels = [label for _index, label in self._zones]
        self.zone_box.configure(values=labels)
        if len(self._zones) == 1:
            self.zone.set(labels[0])
        else:
            self.zone.set("")
        self.query = None
        char_id = _selected_role(self.role.get(), self._roles)
        if char_id is not None:
            self.store.bind_scope(self.account.get().strip(), char_id)
        self._set_status("登录已通过。请选择区服并连接。")
        self._refresh_view()

    def _on_connect(self) -> None:
        zone_index = _selected_zone(self.zone.get(), self._zones)
        if zone_index is None:
            self._set_status("请选择区服。")
            return
        self.session.cancel_requested = False
        self._busy = True
        self._set_status("正在连接区服。")
        self._refresh_view()

        async def action():
            await self.session.connect(zone_index, self._silent)
            self.engine = EggEngine(
                ContextTransport(self.session.context, lambda: False, self._silent),
                store=self.store,
                log=self._silent,
            )

        self._submit(action(), lambda _value: self._set_idle("已连接，尚未开始。"), "连接没有完成。")

    def _on_cancel(self) -> None:
        self.session.request_cancel()
        future = self._future
        if future is not None:
            future.cancel()
        self._op += 1
        self._busy = False
        self._set_status("已取消。")
        self._refresh_view()

    def _on_disconnect(self) -> None:
        if self.engine is not None:
            self.engine.request_abort()

        async def action():
            if self.engine is not None:
                self.engine.transport.close()
            self.engine = None
            await self.session.disconnect()

        self.running = False
        self.query = None
        self._submit(action(), lambda _value: self._set_idle("已断开。"))

    def _on_query(self) -> None:
        engine = self.engine
        if engine is None:
            self._set_status("请先连接区服。")
            return
        engine.begin()
        self._busy = True
        self._set_status("正在查看孵化器。")
        self._refresh_view()

        async def action():
            return await engine.query()

        def done(view):
            self.query = view
            if view.ok:
                self._set_status("已看过孵化器。可以开始，或只收取已经孵好的。")
            else:
                self._set_status("这次没看清孵化器，没有把它当成空的。")
            self._refresh_view()

        self._submit(action(), done, "这次没看清孵化器，没有把它当成空的。")

    def _on_start(self) -> None:
        engine = self.engine
        if engine is None or self.query is None or not self.query.ok:
            self._set_status("请先成功查看一次孵化器。")
            return
        if not messagebox.askokcancel("确认开始", start_confirmation(self.query)):
            self._set_status("已取消，没有开始。")
            return
        engine.begin()
        self.running = True
        self._set_status("正在自动孵化。一次只用一个空位。")
        self._refresh_view()

        async def action():
            report = await engine.run(confirmed=True, retry_uncertain_collect=False)
            if self.session.connected and not engine.abort_requested:
                self.query = await engine.query()
            return report

        def done(report):
            self.running = False
            confirmed = len(engine.collected_ids)
            unconfirmed = len(engine.absent_unconfirmed)
            self._seen_unconfirmed = unconfirmed
            self._set_status(run_status(report.kind, confirmed, unconfirmed))
            self._refresh_view()

        self._submit(action(), done, "自动孵化没有完成。当前状态未知，没有记为成功。")

    def _on_stop(self) -> None:
        if self.engine is not None:
            self.engine.request_stop()
        self._set_status("正在停止。不再开始新的孵化或收取。")

    def _on_collect(self) -> None:
        engine = self.engine
        if engine is None or not self.session.connected:
            self._set_status("请先连接区服。")
            return
        if not messagebox.askokcancel("确认收取", collect_confirmation()):
            self._set_status("已取消，没有收取。")
            return
        engine.begin()
        self.running = True
        self._set_status("正在收取已经孵好的蛋。")
        self._refresh_view()

        async def action():
            report = await engine.collect_ready(confirmed=True, retry_uncertain_collect=False)
            if self.session.connected and not engine.abort_requested:
                self.query = await engine.query()
            return report

        def done(report):
            self.running = False
            confirmed = len(engine.collected_ids)
            unconfirmed = len(engine.absent_unconfirmed)
            self._seen_unconfirmed = unconfirmed
            self._set_status(run_status(report.kind, confirmed, unconfirmed))
            self._refresh_view()

        self._submit(action(), done, "收取没有完成。没有记为成功。")

    def _on_close(self) -> None:
        if self.engine is not None:
            self.engine.request_abort()
        future = self._call(self.session.disconnect())
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.root.destroy()

    def _set_idle(self, text: str) -> None:
        self.running = False
        self._set_status(text)
        self._refresh_view()

    def _submit(self, coro, done, failure_status: str) -> None:
        self._op += 1
        token = self._op
        future = self._call(coro)
        self._future = future

        def poll() -> None:
            if token != self._op:
                return
            if not future.done():
                self.root.after(100, poll)
                return
            self._busy = False
            self._future = None
            try:
                value = future.result()
            except asyncio.CancelledError:
                self._set_status("已取消。")
                self._refresh_view()
                return
            except CancelledStep:
                self._set_status("已取消。")
                self._refresh_view()
                return
            except Exception as exc:
                self.running = False
                self._set_status(visible_failure(exc) if failure_status == "" else failure_status)
                self._refresh_view()
                return
            done(value)

        self.root.after(100, poll)


def _field(parent, row: int, label: str, variable: tk.StringVar, *, secret: bool) -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
    entry = ttk.Entry(parent, textvariable=variable, width=44, show="*" if secret else "")
    entry.grid(row=row, column=1, sticky=tk.W)


def _enabled(value: bool) -> str:
    return tk.NORMAL if value else tk.DISABLED


def _selected_role(text: str, roles: list[tuple[int, str]]) -> int | None:
    for char_id, nickname in roles:
        if text == f"{nickname}（{char_id}）":
            return char_id
    return None


def _selected_zone(text: str, zones: list[tuple[int, str]]) -> int | None:
    for zone_index, label in zones:
        if text == label:
            return zone_index
    return None


def _set_text(widget: tk.Text, text: str) -> None:
    widget.configure(state=tk.NORMAL)
    widget.delete("1.0", tk.END)
    widget.insert(tk.END, text)
    widget.configure(state=tk.DISABLED)


def main() -> None:
    root = tk.Tk()
    UserApp(root)
    root.mainloop()
