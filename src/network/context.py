"""Shared application context and single-reader message distribution."""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
from asyncio import QueueEmpty
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.accounts.login import LoginResult, ZoneData
from src.config import PROJECT_ROOT
from src.game.battle.auto_battle import AutoBattle
from src.game.battle.state import BattleLifecycle
from src.network.socket_client import GameSocket

MessagePredicate = Callable[[dict], bool]
ContextLogger = Callable[[str], None]
MessageObserver = Callable[[dict], None]
DisconnectObserver = Callable[[str], None]
logger = logging.getLogger(__name__)
_BATTLE_TRACE_EXTENSION_IDS = frozenset({13, 15, 16})
_INITIALIZATION_TRACE_COMMANDS = frozenset(
    {"getCurrentTime", "11_1r", "getStartInfo", "10-0"}
)
_battle_receive_log_path: Path | None = None
_battle_receive_log_lock = threading.Lock()
_battle_receive_log_error_reported = False
_battle_receive_record_filter: Callable[[dict], dict] | None = None


def _is_battle_trace_message(message: dict) -> bool:
    if message.get("_ext_id") in _BATTLE_TRACE_EXTENSION_IDS:
        return True

    cmd = str(message.get("_cmd", ""))
    if cmd == "preFightLoad":
        return True
    return cmd.isdigit() and 2300 <= int(cmd) <= 2499


def _battle_trace_json_default(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes_hex": bytes(value).hex()}
    return str(value)


def configure_battle_receive_logging(
    log_directory: Path | None = None, *, record_filter: Callable[[dict], dict] | None = None,
) -> Path:
    """Create one flushed JSONL file for decoded battle receive messages."""
    global _battle_receive_log_path
    global _battle_receive_record_filter
    _battle_receive_record_filter = record_filter
    if _battle_receive_log_path is not None:
        return _battle_receive_log_path

    directory = Path(log_directory) if log_directory is not None else PROJECT_ROOT / "logs"
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"aola-recv-{stamp}.jsonl"
        path.touch(exist_ok=True)
    except OSError:
        if log_directory is not None:
            raise
        directory = Path(tempfile.gettempdir()) / "AolaLoader" / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"aola-recv-{stamp}.jsonl"
        path.touch(exist_ok=True)
    _battle_receive_log_path = path
    return path


def _append_battle_receive_trace(message: dict) -> None:
    global _battle_receive_log_error_reported
    path = _battle_receive_log_path
    if path is None:
        return
    try:
        now = datetime.now().astimezone()
        record = {
            "timeUtc": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timeLocal": now.isoformat(),
            "direction": "接收",
            "decoded": True,
            "type": 1,
            "cmd": message.get("_cmd"),
            "param": message,
        }
        line = json.dumps(
            _battle_receive_record_filter(record) if _battle_receive_record_filter else record,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_battle_trace_json_default,
        )
        with _battle_receive_log_lock:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
        _battle_receive_log_error_reported = False
    except OSError:
        if not _battle_receive_log_error_reported:
            _battle_receive_log_error_reported = True
            logger.exception("无法写入战斗接收日志")


class MessageSubscription:
    def __init__(self, router: "MessageRouter", queue: asyncio.Queue[dict]):
        self._router = router
        self._queue = queue
        self._closed = False

    async def wait_for(
        self, predicate: MessagePredicate, timeout: float | None = None
    ) -> dict:
        async def receive_matching() -> dict:
            while True:
                message = await self._queue.get()
                if predicate(message):
                    return message

        if timeout is None:
            return await receive_matching()
        return await asyncio.wait_for(receive_matching(), timeout=timeout)

    def close(self) -> None:
        if not self._closed:
            self._router.unsubscribe(self._queue)
            self._closed = True

    def drain_pending(self) -> int:
        """Drop messages already queued before a new request is sent."""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except QueueEmpty:
                return dropped
            dropped += 1


class MessageRouter:
    """Own the only receive loop and fan messages out to selected scripts."""

    def __init__(self, socket: GameSocket):
        self.socket = socket
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._observers: set[MessageObserver] = set()
        self._disconnect_observers: set[DisconnectObserver] = set()
        self.message_filter: Callable[[dict], bool] | None = None
        self._task: asyncio.Task[None] | None = None
        self.disconnected = asyncio.Event()
        self.disconnect_reason = ""
        self.last_receive_error = ""

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.disconnected.clear()
            self.disconnect_reason = ""
            self.last_receive_error = ""
            self._task = asyncio.create_task(
                self._receive_loop(), name="game-message-router"
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def subscribe(self, max_queue_size: int = 100) -> MessageSubscription:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers.add(queue)
        return MessageSubscription(self, queue)

    def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        self._subscribers.discard(queue)

    def add_observer(self, observer: MessageObserver) -> None:
        self._observers.add(observer)

    def remove_observer(self, observer: MessageObserver) -> None:
        self._observers.discard(observer)

    def add_disconnect_observer(self, observer: DisconnectObserver) -> None:
        self._disconnect_observers.add(observer)

    def remove_disconnect_observer(self, observer: DisconnectObserver) -> None:
        self._disconnect_observers.discard(observer)

    def publish(self, message: dict) -> None:
        """Publish one decoded message to all current subscribers."""
        if self.message_filter is not None and self.message_filter(message):
            return
        cmd = str(message.get("_cmd", ""))
        if cmd in _INITIALIZATION_TRACE_COMMANDS:
            logger.info(
                "RX INIT: cmd=%s keys=%s",
                cmd,
                ",".join(sorted(str(key) for key in message)),
            )
        if _is_battle_trace_message(message):
            logger.info(
                "RX BATTLE: %s",
                json.dumps(
                    message,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=_battle_trace_json_default,
                ),
            )
            _append_battle_receive_trace(message)
        for observer in tuple(self._observers):
            try:
                observer(message)
            except Exception:
                logger.exception("message observer failed")
        for queue in tuple(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    async def _receive_loop(self) -> None:
        try:
            while self.socket.connected:
                try:
                    message = await self.socket.recv_message(timeout=5.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_receive_error = (
                        f"收包解析异常: {type(exc).__name__}: {exc}"
                    )
                    logger.exception(self.last_receive_error)
                    # The bad packet has already been consumed. Keep the live
                    # transport running instead of reporting a false disconnect.
                    if self.socket.connected:
                        continue
                    break
                if message is None:
                    continue
                self.publish(message)
        except asyncio.CancelledError:
            raise
        finally:
            self.disconnect_reason = (
                self.socket.disconnect_reason
                or self.last_receive_error
                or "接收循环已停止"
            )
            logger.warning("message router stopped: %s", self.disconnect_reason)
            self.disconnected.set()
            for observer in tuple(self._disconnect_observers):
                try:
                    observer(self.disconnect_reason)
                except Exception:
                    logger.exception("disconnect observer failed")


class SessionClock:
    """Own the official per-account 55_2 cadence without catch-up bursts."""

    def __init__(self, context: "AppContext") -> None:
        self.context = context
        self.initial_time = 0
        self.started_at = 0.0
        self.interval = 60.0
        self._task: asyncio.Task[None] | None = None

    def start(self, initial_time: int) -> None:
        self.stop_now()
        self.initial_time = initial_time
        self.started_at = asyncio.get_running_loop().time()
        self._task = asyncio.create_task(
            self._run(), name=f"session-clock-{self.context.user_id}"
        )

    def stop_now(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    async def stop(self) -> None:
        task = self._task
        self.stop_now()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def apply_correction(self, delta: int) -> None:
        # Official H5 handles 55_2.cw as a delta added to leftMinutes.
        # Adjusting the baseline preserves the existing minute deadline.
        self.initial_time = max(0, self.initial_time + delta)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_deadline = self.started_at + self.interval
        try:
            while True:
                await asyncio.sleep(max(0.0, next_deadline - loop.time()))
                if not self.context.socket.connected:
                    return
                elapsed = max(self.interval, loop.time() - self.started_at)
                elapsed_minutes = max(1, int(elapsed // self.interval))
                current = max(0, self.initial_time - elapsed_minutes)
                await self.context.socket.send_xt_message(18, "55_2", {"time": current})
                next_deadline = self.started_at + (elapsed_minutes + 1) * self.interval
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"会话计时发送失败：{exc}"
            self.context.automation_block_reason = reason
            self.context.log(reason)
            logger.warning(reason)


@dataclass
class AppContext:
    config: dict
    login_result: LoginResult
    zone: ZoneData
    socket: GameSocket
    messages: MessageRouter
    log_callback: ContextLogger | None = None
    player_initialized: bool = False
    player_initialization_error: str = ""
    player_initialization_task: asyncio.Task[None] | None = None
    automation_block_reason: str = ""
    battle: BattleLifecycle = field(init=False)
    auto_battle: AutoBattle = field(init=False)
    session_clock: SessionClock = field(init=False)

    def __post_init__(self) -> None:
        self.battle = BattleLifecycle()
        self.messages.message_filter = self.battle.is_stale
        self.auto_battle = AutoBattle(self)
        self.session_clock = SessionClock(self)
        self.messages.add_observer(self._observe_message)
        self.messages.add_disconnect_observer(self._observe_disconnect)

    @property
    def user_id(self) -> str:
        return self.login_result.user_id

    def log(self, message: object) -> None:
        """Emit one user-facing script message when a UI logger is attached."""
        if self.log_callback is not None:
            self.log_callback(str(message))

    def _observe_message(self, message: dict) -> None:
        self.battle.observe(message)
        self.auto_battle.observe(message)
        cmd = str(message.get("_cmd", ""))
        if cmd in {"joinKO", "logKO"}:
            self.socket.active_room_id = -1
            self.socket.active_room_name = ""
            if self.battle.phase != "idle":
                self.battle.mark_unknown(f"战斗期间收到 {cmd}")
        elif cmd == "joinOK" and self.battle.phase in {"active", "ending"}:
            room_id = message.get("room_id")
            if room_id != self.battle.room_id:
                self.battle.mark_unknown("战斗期间房间已改变")

        if cmd == "cw" or (cmd == "55_2" and "cw" in message):
            correction = message.get("cw")
            if isinstance(correction, int) and not isinstance(correction, bool):
                self.session_clock.apply_correction(correction)
            self.automation_block_reason = "服务器返回会话安全指令 cw，已停止自动发送"
            self.log(self.automation_block_reason)
            logger.warning(self.automation_block_reason)

    def _observe_disconnect(self, reason: str) -> None:
        self.auto_battle.disable()
        self.socket.active_room_id = -1
        self.socket.active_room_name = ""
        if self.battle.phase != "idle":
            self.battle.mark_unknown(reason or "账号连接已断开")
        self.session_clock.stop_now()

    def assert_automation_allowed(self) -> None:
        if self.automation_block_reason:
            raise RuntimeError(self.automation_block_reason)

    async def enter_room(self, room_name: str, timeout: float = 15.0) -> None:
        """Create/join one H5 room and wait for the server-assigned room ID."""
        socket = self.socket
        if (
            getattr(socket, "active_room_name", "") == room_name
            and getattr(socket, "active_room_id", -1) > 0
        ):
            self.log(
                f"复用动态房间 {room_name}（roomId={socket.active_room_id}）"
            )
            return

        # A new join attempt invalidates the previous room context until the
        # server confirms the new dynamic ID.
        socket.active_room_id = -1
        socket.active_room_name = ""

        subscription = self.messages.subscribe(max_queue_size=20)
        try:
            await socket.send_xt_message(
                1,
                "cmdCreateAndJoinRoom",
                {"room": room_name},
            )
            try:
                response = await subscription.wait_for(
                    lambda message: str(message.get("_cmd", ""))
                    in {"joinOK", "joinKO"},
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                raise ConnectionError(f"进入房间 {room_name} 超时") from exc

            cmd = str(response.get("_cmd", ""))
            if cmd == "joinKO":
                raise ConnectionError(
                    f"进入房间 {room_name} 失败：{response.get('msg', '未知原因')}"
                )
            if response.get("parse_error"):
                raise ConnectionError(
                    f"进入房间 {room_name} 失败：{response['parse_error']}"
                )
            if response.get("msg"):
                raise ConnectionError(f"进入房间 {room_name} 失败：{response['msg']}")
            room_id = response.get("room_id")
            if not isinstance(room_id, int) or room_id <= 0:
                raise ConnectionError(f"进入房间 {room_name} 未返回有效房间 ID")
            response_room_name = str(response.get("room_name") or room_name)
            if response_room_name != room_name:
                raise ConnectionError(
                    "joinOK 房间名不匹配："
                    f"expected={room_name}, actual={response_room_name}"
                )
            header_room_id = response.get("_room_id", -1)
            if header_room_id != -1 and (
                isinstance(header_room_id, bool)
                or not isinstance(header_room_id, int)
                or header_room_id <= 0
                or header_room_id != room_id
            ):
                raise ConnectionError(
                    "joinOK 房间 ID 不一致："
                    f"header={header_room_id}, payload={room_id}"
                )
            socket.active_room_id = room_id
            socket.active_room_name = response_room_name
            logger.info(
                "room joined: name=%s, room_id=%d",
                socket.active_room_name,
                room_id,
            )
        finally:
            subscription.close()

    async def wait_for_player_initialization(self) -> None:
        task = self.player_initialization_task
        if task is not None:
            await asyncio.shield(task)
        if not self.player_initialized:
            raise ConnectionError(
                self.player_initialization_error or "玩家资料尚未初始化"
            )

    async def close(self) -> None:
        self.auto_battle.disable()
        task = self.player_initialization_task
        self.player_initialization_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.session_clock.stop()
        self.messages.remove_observer(self._observe_message)
        self.messages.remove_disconnect_observer(self._observe_disconnect)
        await self.messages.stop()
        await self.socket.close()
