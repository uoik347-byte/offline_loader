"""Chinese window for querying, hatching and collecting eggs."""
from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from src.config import PROJECT_ROOT
from src.game.eggs.engine import EggEngine
from src.game.eggs.presenter import can_start, confirmation_text, format_board
from src.game.eggs.protocol import public_error
from src.game.eggs.session import CancelledStep, EggSession, format_zone_choice
from src.game.eggs.store import JsonHoldStore
from src.game.eggs.transport import ContextTransport


class EggApp:
    def __init__(self, root: tk.Tk, store_path: Path | None = None, log_path: Path | None = None) -> None:
        self.root = root
        self.root.title("亚比蛋孵化收取")
        self.root.geometry("820x680")
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
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._log_path = log_path or (PROJECT_ROOT / "logs" / "egg-hatch-ui.log")
        self._build()
        self._refresh_view()
        self._log("程序已打开，当前空闲。不会自动孵化或收取。请先查询角色。")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _build(self) -> None:
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        login = ttk.LabelFrame(frame, text="登录", padding=8)
        login.pack(fill=tk.X)
        self.account = tk.StringVar()
        self.password = tk.StringVar()
        self.role = tk.StringVar()
        self.zone = tk.StringVar()
        self.status = tk.StringVar(value="空闲：尚未查询角色")
        _field(login, 0, "账号", self.account, secret=False)
        _field(login, 1, "密码", self.password, secret=True)
        ttk.Label(login, text="角色").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.role_box = ttk.Combobox(login, textvariable=self.role, state="readonly", width=42)
        self.role_box.grid(row=2, column=1, sticky=tk.W)
        ttk.Label(login, text="区服").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.zone_box = ttk.Combobox(login, textvariable=self.zone, state="readonly", width=42)
        self.zone_box.grid(row=3, column=1, sticky=tk.W)
        buttons = ttk.Frame(login)
        buttons.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=6)
        self.lookup_button = ttk.Button(buttons, text="查询角色", command=self._on_lookup)
        self.auth_button = ttk.Button(buttons, text="验证登录", command=self._on_auth)
        self.connect_button = ttk.Button(buttons, text="连接区服", command=self._on_connect)
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._on_cancel)
        self.disconnect_button = ttk.Button(buttons, text="断开", command=self._on_disconnect)
        self.lookup_button.pack(side=tk.LEFT, padx=(0, 6))
        self.auth_button.pack(side=tk.LEFT, padx=(0, 6))
        self.connect_button.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_button.pack(side=tk.LEFT, padx=(0, 6))
        self.disconnect_button.pack(side=tk.LEFT)

        work = ttk.LabelFrame(frame, text="蛋", padding=8)
        work.pack(fill=tk.BOTH, expand=True, pady=8)
        actions = ttk.Frame(work)
        actions.pack(fill=tk.X)
        self.query_button = ttk.Button(actions, text="查询列表", command=self._on_query)
        self.start_button = ttk.Button(actions, text="开始", command=self._on_start)
        self.stop_button = ttk.Button(actions, text="停止", command=self._on_stop)
        self.clear_button = ttk.Button(actions, text="确认格子已空", command=self._on_clear_holds)
        self.query_button.pack(side=tk.LEFT, padx=(0, 6))
        self.start_button.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_button.pack(side=tk.LEFT, padx=(0, 6))
        self.clear_button.pack(side=tk.LEFT)
        self.retry_collect = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            work,
            text="允许再收取一次上次结果不确定、且列表里仍然存在的蛋",
            variable=self.retry_collect,
        ).pack(anchor=tk.W, pady=4)
        ttk.Label(work, textvariable=self.status).pack(anchor=tk.W)
        self.board = tk.Text(work, height=12, wrap=tk.WORD)
        self.board.pack(fill=tk.BOTH, expand=True, pady=4)
        self.log_box = tk.Text(frame, height=10, wrap=tk.WORD)
        self.log_box.pack(fill=tk.BOTH, expand=True)
        for widget in (self.board, self.log_box):
            widget.configure(state=tk.DISABLED)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _refresh_view(self) -> None:
        connected = self.session.connected
        verified = self.session.password_verified
        self.lookup_button.configure(state=_enabled(not self._busy and not self.running))
        self.auth_button.configure(state=_enabled(bool(self._roles) and not self._busy and not self.running))
        self.connect_button.configure(
            state=_enabled(verified and bool(self._zones) and not self._busy and not self.running)
        )
        self.cancel_button.configure(state=_enabled(self._busy))
        self.query_button.configure(state=_enabled(connected and not self.running and not self._busy))
        self.start_button.configure(
            state=_enabled(can_start(connected, self.query, self.store.ok, self.running or self._busy))
        )
        self.stop_button.configure(state=_enabled(self.running))
        self.disconnect_button.configure(state=_enabled((connected or self.running) and not self._busy))
        _set_text(self.board, format_board(self.query, self.store))

    def _log(self, message: str) -> None:
        _set_text(self.log_box, message + "\n", append=True)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(message + "\n")
        except OSError:
            pass

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _on_lookup(self) -> None:
        account = self.account.get().strip()
        if not account:
            self._log("请填写账号。查询角色不会发送密码。")
            return
        self.session.cancel_requested = False
        self._busy = True
        self.status.set("正在查询角色，尚未验证密码")
        self._refresh_view()

        async def action():
            return await self.session.lookup_roles(account, self._thread_log)

        self._submit(action(), self._lookup_done, "角色查询失败")

    def _lookup_done(self, roles) -> None:
        self._roles = [(role.char_id, role.nickname) for role in roles]
        labels = [f"{nickname}（{char_id}）" for char_id, nickname in self._roles]
        self.role_box.configure(values=labels)
        if len(self._roles) == 1:
            self.role.set(labels[0])
            self.status.set("已找到 1 个角色并自动选中。密码尚未验证。")
        else:
            self.role.set("")
            self.status.set(f"已找到 {len(self._roles)} 个角色，请选择后再验证密码。")
        self._zones = []
        self.zone_box.configure(values=[])
        self.zone.set("")
        self.query = None
        self._refresh_view()

    def _on_auth(self) -> None:
        char_id = _selected_role(self.role.get(), self._roles)
        if char_id is None:
            self._log("请先选择角色。查询成功并不表示密码已验证。")
            return
        account = self.account.get().strip()
        password = self.password.get()
        self.password.set("")
        if not password:
            self._log("请填写密码。角色查询成功并不表示密码已验证。")
            return
        self.session.cancel_requested = False
        self._busy = True
        self.status.set("正在验证账号密码")
        self._refresh_view()

        async def action():
            return await self.session.authenticate(account, password, char_id, self._thread_log)

        self._submit(action(), self._auth_done, "账号密码验证失败")

    def _auth_done(self, zones) -> None:
        self._zones = [(zone.zone_index, format_zone_choice(zone)) for zone in zones]
        labels = [label for _index, label in self._zones]
        self.zone_box.configure(values=labels)
        if len(self._zones) == 1:
            self.zone.set(labels[0])
        else:
            self.zone.set("")
        self.query = None
        self.status.set("密码已验证。请选择区服后再连接。")
        self._refresh_view()

    def _on_connect(self) -> None:
        zone_index = _selected_zone(self.zone.get(), self._zones)
        if zone_index is None:
            self._log("请选择本次登录返回的区服。")
            return
        self.session.cancel_requested = False
        self._busy = True
        self.status.set("正在连接区服")
        self._refresh_view()

        async def action():
            await self.session.connect(zone_index, self._thread_log)
            self.engine = EggEngine(
                ContextTransport(self.session.context, lambda: False, self._thread_log),
                store=self.store,
                log=self._thread_log,
            )

        self._submit(action(), lambda _value: self._set_idle("已连接，空闲"), "区服长连接失败")

    def _on_cancel(self) -> None:
        self.session.request_cancel()
        future = self._future
        if future is not None:
            future.cancel()
        self._op += 1
        self._busy = False
        self.status.set("已取消，没有继续下一步")
        self._log("已取消当前步骤。")
        self._refresh_view()

    def _on_disconnect(self) -> None:
        if self.engine is not None:
            self.engine.request_stop()

        async def action():
            if self.engine is not None:
                self.engine.transport.close()
            self.engine = None
            await self.session.disconnect()

        self.running = False
        self.query = None
        self._submit(action(), lambda _value: self._set_idle("已断开，空闲"))

    def _on_query(self) -> None:
        engine = self.engine
        if engine is None:
            self._log("请先连接。")
            return
        self.status.set("正在查询")

        async def action():
            return await engine.query()

        def done(view):
            self.query = view
            self.status.set("查询完成，空闲" if view.ok else "查询未完成，空闲")
            self._refresh_view()

        self._submit(action(), done)

    def _on_start(self) -> None:
        engine = self.engine
        if engine is None or self.query is None or not self.query.ok:
            self._log("请先完成一次成功的查询。")
            return
        if not messagebox.askokcancel("确认开始", confirmation_text(self.query, self.store, self.retry_collect.get())):
            self._log("已取消。没有发送孵化或收取。")
            return
        engine.stop_requested = False
        self.running = True
        self.status.set("正在运行")
        self._refresh_view()
        retry = self.retry_collect.get()

        async def action():
            report = await engine.run(confirmed=True, retry_uncertain_collect=retry)
            self.query = await engine.query() if self.session.connected and not engine.stop_requested else self.query
            return report

        def done(report):
            self.running = False
            self.status.set("已停止，空闲" if report.stopped else "运行结束，空闲")
            self._log(report.detail)
            self._refresh_view()

        self._submit(action(), done)

    def _on_stop(self) -> None:
        if self.engine is not None:
            self.engine.request_stop()
        self.status.set("正在停止，不再发送新的指令")
        self._log("已按下停止。已经提交的蛋会留在游戏中。")

    def _on_clear_holds(self) -> None:
        if self.running:
            self._log("运行中不能清除占用记录。")
            return
        if not messagebox.askokcancel(
            "确认格子已空",
            "这只清除本机记住的孵化格占用，不会向服务器发送指令。\n"
            "如果游戏里的格子其实还有蛋，下一次查询会再把它们显示出来。",
        ):
            return
        self.store.clear_slot_holds()
        self._log("已清除本机格子占用记录。没有发送指令。")
        self._refresh_view()

    def _on_close(self) -> None:
        if self.engine is not None:
            self.engine.request_stop()
        future = self._call(self.session.disconnect())
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.root.destroy()

    def _set_idle(self, text: str) -> None:
        self.running = False
        self.status.set(text)
        self._refresh_view()

    def _thread_log(self, message: str) -> None:
        self.root.after(0, lambda: self._log(message))

    def _submit(self, coro, done, failure_status: str = "出现错误，空闲") -> None:
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
                self.status.set("已取消，没有继续下一步")
                self._refresh_view()
                return
            except CancelledStep as exc:
                self.status.set("已取消，没有继续下一步")
                self._log(str(exc))
                self._refresh_view()
                return
            except Exception as exc:
                self.running = False
                self.status.set(failure_status)
                self._log(public_error(exc))
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


def _set_text(widget: tk.Text, text: str, *, append: bool = False) -> None:
    widget.configure(state=tk.NORMAL)
    if not append:
        widget.delete("1.0", tk.END)
    widget.insert(tk.END, text)
    widget.configure(state=tk.DISABLED)


def main() -> None:
    root = tk.Tk()
    EggApp(root)
    root.mainloop()
