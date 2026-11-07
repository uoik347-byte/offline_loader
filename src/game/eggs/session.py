"""Direct account login for the egg tool.

Uses the repository's normal HTTP login and game socket. Proxy settings and
``config.json`` are not used. The password is not kept on this object.
"""
from __future__ import annotations

import os
from typing import Callable

from src.accounts.login import http_login
from src.accounts.session import select_zone, start_player_initialization
from src.network.context import AppContext, MessageRouter
from src.network.socket_client import login_and_connect


Log = Callable[[str], None]
_PROXY_ENV = ("AOLA_PROXY_URL", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


class EggSession:
    def __init__(self) -> None:
        self.context: AppContext | None = None
        self._login = None
        self._char_id = 0

    @property
    def connected(self) -> bool:
        context = self.context
        return bool(
            context is not None
            and context.socket.connected
            and not context.messages.disconnected.is_set()
        )

    async def authenticate(self, account: str, password: str, char_id: int, log: Log) -> list[tuple[int, str]]:
        if not isinstance(account, str) or not account.strip():
            raise ValueError("请填写账号")
        if not isinstance(password, str) or not password:
            raise ValueError("请填写密码")
        if isinstance(char_id, bool) or not isinstance(char_id, int) or char_id < 0:
            raise ValueError("角色 ID 必须是非负整数")
        if any(os.environ.get(name) for name in _PROXY_ENV):
            log("检测到代理环境变量。本工具只使用直接连接。")
        result = await http_login(
            account.strip(),
            password,
            char_id=char_id,
            browser_verification=False,
            network_mode="system",
            timeout=30,
            save_diagnostics=False,
        )
        if not result.success:
            reason = result.blocked_reason or result.error_msg or "登录失败"
            raise ConnectionError(reason)
        zones = [
            (zone.zone_index, zone.zone_name or f"区服 {zone.zone_index}")
            for zone in result.zone_list
            if (zone.host and zone.flash_port > 0) or (zone.domain and zone.port > 0)
        ]
        if not zones:
            raise ConnectionError("登录成功，但没有可用区服")
        self._login = result
        self._char_id = char_id
        log(f"登录完成，可选区服 {len(zones)} 个。请选择区服后再连接。")
        return zones

    async def connect(self, zone_index: int, log: Log) -> None:
        result = self._login
        if result is None:
            raise RuntimeError("请先登录")
        if isinstance(zone_index, bool) or not isinstance(zone_index, int):
            raise ValueError("请选择区服")
        zone = select_zone(result, zone_index)
        if zone is None:
            raise ConnectionError(f"指定区服 {zone_index} 不存在或没有可用的直接连接地址")
        if self.context is not None:
            await self.disconnect()
        socket = await login_and_connect(
            zone,
            result.user_id,
            result.session_id,
            timeout=15.0,
            tcp_retry_window=10.0,
            network_mode="system",
        )
        if socket is None:
            raise ConnectionError("游戏长连接登录失败")
        router = MessageRouter(socket)
        context = AppContext(
            {
                "char_id": self._char_id,
                "zone_index": zone.zone_index,
                "network_mode": "system",
                "timeout": 15,
            },
            result,
            zone,
            socket,
            router,
        )
        router.start()
        start_player_initialization(context, timeout=15)
        try:
            await context.wait_for_player_initialization()
        except Exception:
            await context.close()
            raise
        self.context = context
        log(f"已连接区服 {zone.zone_index} {zone.zone_name}。当前空闲。")

    async def disconnect(self) -> None:
        context = self.context
        self.context = None
        if context is not None:
            await context.close()

    def forget_login(self) -> None:
        self._login = None
