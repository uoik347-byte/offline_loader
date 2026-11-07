"""Send one egg command and accept only a reply that arrives after that send."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from src.game.eggs.protocol import format_recv, format_send, public_error


Matcher = Callable[[dict], bool]


@dataclass
class Exchange:
    kind: str
    message: dict | None = None
    sent: bool = False
    detail: str = ""


class ContextTransport:
    """Uses the existing account socket. Replies already queued are discarded."""

    def __init__(self, context, stop_check: Callable[[], bool], log: Callable[[str], None]):
        self._context = context
        self._stop_check = stop_check
        self.should_stop = None
        self.allow_command = None
        self._log = log
        self._subscription = None

    def _stopped(self) -> bool:
        checker = self.should_stop or self._stop_check
        return bool(checker and checker())

    def open(self) -> None:
        if self._subscription is None:
            self._subscription = self._context.messages.subscribe(max_queue_size=2000)

    def close(self) -> None:
        subscription = self._subscription
        self._subscription = None
        if subscription is not None:
            subscription.close()

    async def exchange(
        self,
        ext_id: int,
        cmd: str,
        param: dict,
        match: Matcher,
        timeout: float,
    ) -> Exchange:
        if self._stopped():
            return Exchange("stopped")
        try:
            self._context.assert_automation_allowed()
        except RuntimeError as exc:
            return Exchange("blocked", detail=public_error(exc))
        if not self._context.socket.connected or self._context.messages.disconnected.is_set():
            return Exchange("disconnected", detail="账号连接已断开")
        self.open()
        assert self._subscription is not None
        self._subscription.drain_pending()
        if self._stopped():
            return Exchange("stopped")
        if self.allow_command is not None and not self.allow_command(ext_id, cmd):
            return Exchange("stopped")
        self._log(format_send(ext_id, cmd, param))
        try:
            await self._context.socket.send_xt_message(ext_id, cmd, dict(param))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return Exchange(
                "disconnected",
                sent=True,
                detail=f"发送没有正常完成：{public_error(exc)}",
            )
        return await self._read(match, timeout, sent=True)

    async def wait(self, match: Matcher, timeout: float) -> Exchange:
        """Wait without sending. Used for the optional invalid-egg notice."""
        if self._subscription is None:
            return Exchange("disconnected", detail="还没有接收通道")
        return await self._read(match, timeout, sent=False)

    async def _read(self, match: Matcher, timeout: float, *, sent: bool) -> Exchange:
        assert self._subscription is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            if not self._context.socket.connected or self._context.messages.disconnected.is_set():
                return Exchange("disconnected", sent=sent, detail="账号连接已断开")
            remaining = deadline - loop.time()
            if remaining <= 0:
                if self._stopped():
                    return Exchange("stopped", sent=sent)
                return Exchange("timeout", sent=sent)
            slice_timeout = min(0.2, remaining)
            try:
                message = await self._subscription.wait_for(
                    lambda _item: True, timeout=slice_timeout
                )
            except asyncio.TimeoutError:
                if self._stopped():
                    return Exchange("stopped", sent=sent)
                continue
            if match(message):
                self._log(format_recv(message))
                return Exchange("message", message=message, sent=sent)
