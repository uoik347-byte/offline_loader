"""Direct account login for the egg tool.

Role lookup sends only the account id. Password authentication is a later
request. The game socket is a third step and uses the login response's user id
and session, not the numeric charId.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from src.accounts.login import LoginResult, RoleInfo, ZoneData, http_login, query_roles, select_role
from src.accounts.session import select_zone, start_player_initialization
from src.game.eggs.protocol import public_error
from src.network.context import AppContext, MessageRouter
from src.network.socket_client import login_and_connect


Log = Callable[[str], None]
_PROXY_ENV = ("AOLA_PROXY_URL", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


class CancelledStep(ConnectionError):
    """The user cancelled before the next login or connect step."""


@dataclass(frozen=True)
class RoleChoice:
    char_id: int
    nickname: str


@dataclass(frozen=True)
class ZoneChoice:
    zone_index: int
    zone_name: str
    has_tcp: bool
    has_h5: bool


def choose_role(roles: list[RoleInfo], char_id: int | None) -> RoleInfo:
    if not roles:
        raise ValueError("账号没有可用角色")
    if char_id is None:
        if len(roles) == 1:
            return roles[0]
        raise ValueError("查询到多个角色，请选择一个后再验证密码")
    return select_role(roles, char_id)


def list_zone_choices(result: LoginResult) -> list[ZoneChoice]:
    choices: list[ZoneChoice] = []
    for zone in result.zone_list:
        has_tcp = bool(zone.host and zone.flash_port > 0)
        has_h5 = bool(zone.domain and zone.port > 0)
        if not has_tcp and not has_h5:
            continue
        choices.append(
            ZoneChoice(
                zone_index=zone.zone_index,
                zone_name=zone.zone_name or "未命名",
                has_tcp=has_tcp,
                has_h5=has_h5,
            )
        )
    return choices


def format_zone_choice(choice: ZoneChoice) -> str:
    ways = []
    if choice.has_tcp:
        ways.append("TCP")
    if choice.has_h5:
        ways.append("H5")
    return f"{choice.zone_index} {choice.zone_name}（{'/'.join(ways)}）"


class EggSession:
    def __init__(self) -> None:
        self.context: AppContext | None = None
        self.cancel_requested = False
        self._login: LoginResult | None = None
        self._char_id = 0
        self._roles: list[RoleInfo] = []

    @property
    def connected(self) -> bool:
        context = self.context
        return bool(
            context is not None
            and context.socket.connected
            and not context.messages.disconnected.is_set()
        )

    @property
    def password_verified(self) -> bool:
        return self._login is not None and self._login.success

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def _check_cancel(self) -> None:
        if self.cancel_requested:
            raise CancelledStep("已取消，没有继续下一步")

    async def lookup_roles(self, account: str, log: Log) -> list[RoleChoice]:
        self._check_cancel()
        if any(os.environ.get(name) for name in _PROXY_ENV):
            log("检测到代理环境变量。本工具只使用直接连接。")
        log("正在查询角色。这一步不发送密码，也不表示密码正确。")
        try:
            roles = await query_roles(account, network_mode="system", timeout=30)
        except Exception as exc:
            self._roles = []
            raise ConnectionError(f"角色查询失败，密码没有发送：{public_error(exc)}") from exc
        self._check_cancel()
        self._roles = list(roles)
        self._login = None
        log(f"角色查询完成，共 {len(roles)} 个。密码尚未验证。")
        return [RoleChoice(role.char_id, role.nickname) for role in roles]

    async def authenticate(self, account: str, password: str, char_id: int, log: Log) -> list[ZoneChoice]:
        self._check_cancel()
        if any(os.environ.get(name) for name in _PROXY_ENV):
            log("检测到代理环境变量。本工具只使用直接连接。")
        if not isinstance(password, str) or not password:
            raise ValueError("请填写密码。角色查询成功并不表示密码已验证。")
        if self._roles:
            known = {role.char_id for role in self._roles}
            if char_id not in known:
                raise ValueError("所选角色不在刚查询到的列表中")
        log("正在验证账号密码。")
        result = await http_login(
            account.strip(),
            password,
            char_id=char_id,
            browser_verification=False,
            network_mode="system",
            timeout=30,
            save_diagnostics=False,
        )
        self._check_cancel()
        if not result.success:
            self._login = None
            reason = result.blocked_reason or result.error_msg or "登录失败"
            raise ConnectionError(f"账号密码验证失败，还没有连接区服：{public_error(ConnectionError(reason))}")
        zones = list_zone_choices(result)
        if not zones:
            self._login = None
            raise ConnectionError("密码已验证，但登录响应里没有可用区服，尚未发起游戏长连接")
        self._login = result
        self._char_id = char_id
        log(f"密码已验证，可选区服 {len(zones)} 个。请选择区服后再连接。")
        return zones

    async def connect(self, zone_index: int, log: Log) -> None:
        self._check_cancel()
        result = self._login
        if result is None or not result.success:
            raise RuntimeError("请先完成账号密码验证")
        if isinstance(zone_index, bool) or not isinstance(zone_index, int):
            raise ValueError("请选择区服")
        zone = select_zone(result, zone_index)
        if zone is None:
            raise ConnectionError(f"区服 {zone_index} 不在本次登录返回的可用列表中，没有发起长连接")
        if self.context is not None:
            await self.disconnect()
        notes: list[str] = []
        log(f"正在连接区服 {zone.zone_index} {zone.zone_name}。")
        socket = await login_and_connect(
            zone,
            result.user_id,
            result.session_id,
            timeout=15.0,
            network_mode="system",
            stage_notes=notes,
        )
        self._check_cancel()
        if socket is None:
            detail = "；".join(notes) if notes else "没有记录到失败阶段"
            summary = result.server_parse_summary or "没有区服解析统计"
            raise ConnectionError(
                f"区服长连接登录失败。{detail}。{format_zone_choice(ZoneChoice(zone.zone_index, zone.zone_name or '未命名', bool(zone.host and zone.flash_port > 0), bool(zone.domain and zone.port > 0)))}。"
                f"解析字段 server_index={zone.server_index} host={zone.host or '空'} "
                f"flash_port={zone.flash_port} h5_port={zone.port} "
                f"domain={zone.domain.strip() or '空'}。{summary}"
            )
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
        except Exception as exc:
            await context.close()
            raise ConnectionError(f"区服长连接已建立，玩家资料初始化失败：{public_error(exc)}") from exc
        self.context = context
        log(f"已连接区服 {zone.zone_index} {zone.zone_name}。当前空闲。")

    async def disconnect(self) -> None:
        context = self.context
        self.context = None
        if context is not None:
            await context.close()

    def forget_login(self) -> None:
        self._login = None
        self._roles = []
