"""
login.py — HTTP 登录、响应解析和服务器列表模型
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from src.accounts.browser_login import browser_login, is_captcha_page
from src.config import PROJECT_ROOT, load_user_config, default_config

@dataclass
class ServerEndpoint:
    server_index: int
    host: str
    flash_port: int
    h5_port: int
    domain: str = ""


def parse_server_list(text: str) -> list[ServerEndpoint]:
    servers, _summary = parse_server_list_report(text)
    return servers


def parse_server_list_report(text: str) -> tuple[list[ServerEndpoint], str]:
    """Parse ``host:flash端口:h5端口:domain``. Other colon counts are counted and ignored."""
    servers: list[ServerEndpoint] = []
    field_counts: dict[int, int] = {}
    bad_ports = 0
    for index, raw in enumerate(text.split(";")):
        entry = raw.strip()
        if not entry:
            continue
        parts = entry.split(":")
        field_counts[len(parts)] = field_counts.get(len(parts), 0) + 1
        if len(parts) != 4:
            continue
        try:
            flash_port = int(parts[1])
            h5_port = int(parts[2])
        except ValueError:
            bad_ports += 1
            continue
        servers.append(ServerEndpoint(index, parts[0], flash_port, h5_port, parts[3]))
    summary = [f"接受 host:flash端口:h5端口:domain 共 {len(servers)} 条"]
    skipped = [
        f"{count} 字段 {field_counts[count]} 条"
        for count in sorted(field_counts)
        if count != 4
    ]
    if skipped:
        summary.append("未按该四段格式忽略 " + "、".join(skipped))
    if bad_ports:
        summary.append(f"端口不是整数 {bad_ports} 条")
    return servers, "；".join(summary)


@dataclass
class ZoneData:
    zone_index: int
    zone_name: str
    host: str
    port: int
    flash_port: int = 0
    user_num: int = 0
    is_new: bool = False
    is_hot: bool = False
    domain: str = ""
    server_index: int = -1


@dataclass
class LoginResult:
    success: bool
    user_id: str = ""
    duoduo_id: str = ""
    session_id: str = ""
    zone_list: list[ZoneData] = field(default_factory=list)
    error_msg: str = ""
    blocked_reason: str = ""
    server_list: list[ServerEndpoint] = field(default_factory=list)
    server_parse_summary: str = ""
    http_stage: str = ""
    http_status: int | None = None


class LoginBlockedError(ConnectionError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class RoleInfo:
    char_id: int
    user_id: str
    nickname: str


def _get_element_text(element, key: str) -> str:
    children = element.findall(key)
    if not children:
        return ""
    child = children[0]
    text = child.text
    if text is None:
        text = ''.join(child.itertext()).strip()
    return text or ""


def md5_password(password: str) -> str:
    return hashlib.md5(password.encode('utf-8')).hexdigest()


def parse_role_response(text: str) -> list[RoleInfo]:
    roles: list[RoleInfo] = []
    seen_char_ids: set[int] = set()
    for index, raw_role in enumerate(text.split("|"), start=1):
        raw_role = raw_role.strip()
        if not raw_role:
            continue
        parts = raw_role.split(",", 3)
        if len(parts) < 3:
            raise ValueError(f"角色列表第 {index} 项格式错误")
        try:
            char_id = int(parts[0])
        except ValueError as exc:
            raise ValueError(f"角色列表第 {index} 项 charId 无效") from exc
        user_id = parts[1].strip()
        nickname = parts[2].strip()
        if char_id < 0 or not user_id or not nickname:
            raise ValueError(f"角色列表第 {index} 项字段无效")
        if char_id in seen_char_ids:
            raise ValueError(f"角色列表包含重复 charId={char_id}")
        seen_char_ids.add(char_id)
        roles.append(RoleInfo(char_id, user_id, nickname))
    if not roles:
        raise ValueError("账号没有可用角色")
    return roles


def select_role(roles: list[RoleInfo], char_id: int) -> RoleInfo:
    role = next((item for item in roles if item.char_id == char_id), None)
    if role is None:
        available = "、".join(str(item.char_id) for item in roles)
        raise ValueError(f"配置的 charId={char_id} 不存在；可用值：{available}")
    return role


def build_login_params(account: str, password: str, char_id: int) -> dict[str, str]:
    return {
        "account": account,
        "password": md5_password(password),
        "charId": str(char_id),
        "logintype": "",
        "wyToken": "",
        "fromurl": "",
        "webSite": "",
        "token": "",
        "cookieId": "",
        "pi": "",
        "sessionId": "",
        "content": "",
    }


def validate_game_login(result: LoginResult, account: str, role: Optional[RoleInfo] = None) -> None:
    if not result.success:
        if result.blocked_reason:
            raise LoginBlockedError(result.blocked_reason, result.error_msg)
        raise ValueError(result.error_msg)
    if result.duoduo_id != account:
        raise ValueError("游戏登录响应的账号与当前账号不匹配")
    if not result.session_id or not result.user_id:
        raise ValueError("游戏登录响应缺少 sid 或角色 ID")
    if role is not None and result.user_id != role.user_id:
        raise ValueError(f"角色校验失败：charId={role.char_id} 的 userId 不匹配")


def parse_login_response(xml_text: str) -> LoginResult:
    try:
        root = ET.fromstring(xml_text)
        result_code = _get_element_text(root, "c")

        if result_code != "ok":
            error_msgs = {
                "freeze": "账号异常，已被冻结！",
                "error": "网络或服务器发生错误",
                "invalid": "用户名或密码错误！",
                "busy": "服务器繁忙",
                "maintain": "系统维护中",
            }
            return LoginResult(
                success=False,
                error_msg=error_msgs.get(result_code, f"Error:{result_code}"),
                blocked_reason="服务器返回 IP 登录限制" if result_code == "ip_limit" else "",
            )

        duoduo_id = _get_element_text(root, "d") or _get_element_text(root, "ddid")
        player_id = _get_element_text(root, "u")
        session_id = _get_element_text(root, "sid")

        svr_text = _get_element_text(root, "svr")
        zn_text = _get_element_text(root, "zn")

        server_list, server_parse_summary = parse_server_list_report(svr_text)
        servers_by_index = {server.server_index: server for server in server_list}
        zone_info_list = [z for z in zn_text.split(";") if z.strip()]

        zone_list = []
        for zone_info in zone_info_list:
            parts = zone_info.split("/")
            if len(parts) < 5:
                continue
            zone_names = parts[0].split(" ")
            zone_index = int(zone_names[0])
            server_idx = int(parts[1])
            if server_idx not in servers_by_index:
                continue
            server = servers_by_index[server_idx]
            host, flash_port, h5_port, domain = server.host, server.flash_port, server.h5_port, server.domain
            if flash_port <= 0 and h5_port <= 0:
                continue
            if zone_index >= 100000:
                continue
            zone_list.append(ZoneData(
                zone_index=zone_index,
                zone_name=zone_names[1] if len(zone_names) > 1 else "",
                host=host,
                port=h5_port,
                flash_port=flash_port,
                user_num=int(parts[6]) if len(parts) > 6 and parts[6].replace('-', '').strip() else 0,
                is_new=parts[3] == "1" if len(parts) > 3 else False,
                is_hot=parts[4] == "1" if len(parts) > 4 else False,
                domain=domain,
                server_index=server_idx,
            ))

        zone_list.sort(key=lambda z: z.user_num)

        return LoginResult(
            success=True,
            user_id=player_id,
            duoduo_id=duoduo_id,
            session_id=session_id,
            zone_list=zone_list,
            server_list=server_list,
            server_parse_summary=server_parse_summary,
        )
    except ET.ParseError as e:
        return LoginResult(success=False, error_msg=f"XML parse error: {e}")


def _save_login_failure(stage, request_url, response, body, error, account, password, diagnostic_dir=None):
    def redact(text):
        for value in (md5_password(password), urllib.parse.quote_plus(password), password, account):
            if value:
                text = text.replace(value, "[REDACTED]")
        text = re.sub(
            r"(<(?:sid|token|sessionId|password|loginkey)\b[^>]*>).*?(</(?:sid|token|sessionId|password|loginkey)\s*>)",
            r"\1[REDACTED]\2", text, flags=re.IGNORECASE | re.DOTALL,
        )
        return re.sub(
            r'''(["']?\b(?:sid|token|session_?id|password|pwd|wyToken|loginkey)["']?\s*[:=]\s*["']?)[^"'\s<>&;,}]+''',
            r"\1[REDACTED]", text, flags=re.IGNORECASE,
        )

    def safe_url(url):
        parts = urllib.parse.urlsplit(str(url))
        return redact(urllib.parse.urlunsplit((
            parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "",
        )))

    text = body.decode("utf-8", errors="replace")
    markers = [marker for marker in ("TCaptcha.js", "TencentCaptcha", "/WafCaptcha")
               if marker.lower() in text.lower()]
    status = response.status if response is not None else None
    if markers:
        analysis = "疑似 WAF 人机验证页面（检测到腾讯验证码标记）"
    elif status == 429:
        analysis = "HTTP 429：请求受到限流"
    elif re.search(r"<!doctype\s+html|<html\b|<script\b", text, re.IGNORECASE):
        analysis = "收到 HTML/脚本页面，而非预期的登录数据"
    elif status is not None and status >= 400:
        analysis = f"HTTP {status}：请求失败"
    else:
        analysis = "角色查询或登录失败，详见错误与响应正文"
    now = datetime.now(timezone.utc)
    output_dir = Path(diagnostic_dir) if diagnostic_dir is not None else (Path(os.environ["AOLA_DIAGNOSTIC_DIR"]) if os.environ.get("AOLA_DIAGNOSTIC_DIR") else PROJECT_ROOT / "evidence" / "login-failures")
    name = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{stage}"
    metadata_path = output_dir / f"{name}.json"
    body_path = output_dir / f"{name}.response.txt"
    metadata = {
        "captured_at_utc": now.isoformat(),
        "stage": stage,
        "method": "POST",
        "request_url": safe_url(request_url),
        "final_url": safe_url(response.url) if response is not None else None,
        "http_status": status,
        "content_type": response.headers.get("Content-Type", "") if response is not None else "",
        "retry_after": response.headers.get("Retry-After", "") if response is not None else "",
        "redirects": [
            {"status": item.status, "url": safe_url(item.url)}
            for item in response.history
        ] if response is not None else [],
        "body_bytes": len(body),
        "response_file": body_path.name,
        "response_redacted": True,
        "analysis": analysis,
        "captcha_markers": markers,
        "error": redact(error),
    }
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        body_path.write_text(redact(text), encoding="utf-8")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return f"{redact(error)}；诊断保存失败：{exc}"
    return f"{redact(error)}；{analysis}；诊断文件：{metadata_path}"


async def query_roles(
    account: str,
    *,
    register_url: Optional[str] = None,
    timeout: Optional[float] = None,
    network_mode: str = "system",
) -> list[RoleInfo]:
    """Look up roles with the account id only. This does not send or check a password."""
    if not isinstance(account, str) or not account.strip():
        raise ValueError("请填写账号")
    account = account.strip()
    conf = {} if network_mode == "system" else load_user_config()
    if register_url is None:
        register_url = conf.get("register_url", default_config()["register_url"])
    request_timeout = aiohttp.ClientTimeout(
        total=float(timeout if timeout is not None else conf.get("timeout", 30))
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://aola.100bt.com",
        "Referer": "https://aola.100bt.com/",
    }
    from src.network.proxy import process_proxy, proxy_connector
    effective_proxy = None if network_mode == "system" else process_proxy()
    session_options = {"connector": proxy_connector(effective_proxy)} if effective_proxy else {}
    if network_mode == "system":
        session_options["trust_env"] = False
    try:
        async with aiohttp.ClientSession(**session_options) as session:
            async with session.post(
                register_url,
                data=urllib.parse.urlencode({"type": "query_role_info", "duoduoId": account}),
                headers=headers,
                timeout=request_timeout,
            ) as response:
                body = await response.read()
                response.raise_for_status()
                text = body.decode("utf-8", errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise ConnectionError(f"角色查询请求失败：{type(exc).__name__}") from exc
    try:
        return parse_role_response(text)
    except ValueError as exc:
        raise ConnectionError(f"角色查询结果无法识别：{exc}") from exc


async def http_login(
    account: str,
    password: str,
    char_id: int = 0,
    login_url: Optional[str] = None,
    register_url: Optional[str] = None,
    browser_verification: bool = False,
    timeout: Optional[float] = None,
    proxy_url: Optional[str] = None,
    network_mode: str = "legacy",
    diagnostic_dir: Optional[str] = None,
    save_diagnostics: bool = True,
) -> LoginResult:
    """Query the configured role and return the selected role's login result."""
    conf = {} if network_mode == "system" else load_user_config()
    request_timeout = aiohttp.ClientTimeout(
        total=float(timeout if timeout is not None else conf.get("timeout", 30))
    )
    if login_url is None:
        login_url = conf.get("login_url", default_config()["login_url"])
    if register_url is None:
        register_url = conf.get(
            "register_url", default_config()["register_url"]
        )
    if isinstance(char_id, bool) or not isinstance(char_id, int) or char_id < 0:
        return LoginResult(success=False, error_msg="charId 必须是非负整数")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://aola.100bt.com",
        "Referer": "https://aola.100bt.com/",
    }

    stage = "role_query"
    request_url = register_url
    response = None
    response_body = b""
    try:
        from src.network.proxy import process_proxy, proxy_connector
        effective_proxy = None if network_mode == "system" else (proxy_url or process_proxy())
        session_options = {"connector": proxy_connector(effective_proxy)} if effective_proxy else {}
        if network_mode == "system":
            session_options["trust_env"] = False
        async with aiohttp.ClientSession(**session_options) as session:
            async def post(url, params, name):
                nonlocal stage, request_url, response, response_body
                stage, request_url, response, response_body = name, url, None, b""
                async with session.post(url, data=urllib.parse.urlencode(params),
                                        headers=headers, timeout=request_timeout) as resp:
                    response = resp
                    response_body = await resp.read()
                    resp.raise_for_status()
                    return response_body.decode("utf-8", errors="replace")

            role_text = await post(register_url, {"type": "query_role_info", "duoduoId": account}, "role_query")
            role = select_role(parse_role_response(role_text), char_id)
            result = parse_login_response(await post(login_url, build_login_params(account, password, char_id), "login"))
            validate_game_login(result, account, role)
    except LoginBlockedError as e:
        result = LoginResult(success=False, error_msg=str(e), blocked_reason=e.reason)
    except ValueError as e:
        result = LoginResult(success=False, error_msg=str(e))
    except aiohttp.ClientError as e:
        result = LoginResult(success=False, error_msg=f"HTTP error: {e}")
    except (ProxyConnectionError, ProxyError, ProxyTimeoutError) as e:
        result = LoginResult(success=False, error_msg=f"Proxy error: {e}")
    except asyncio.TimeoutError:
        result = LoginResult(success=False, error_msg="Login request timed out")
    result.http_stage = stage
    result.http_status = response.status if response is not None else None
    if not result.success:
        status = response.status if response is not None else None
        if status in (403, 429):
            result.blocked_reason = f"HTTP {status}：服务器拒绝继续登录"
        elif is_captcha_page(response_body.decode("utf-8", errors="replace")):
            result.blocked_reason = "登录需要人机验证"
        if save_diagnostics:
            result.error_msg = _save_login_failure(
                stage, request_url, response, response_body, result.error_msg, account, password,
                **({"diagnostic_dir": diagnostic_dir} if diagnostic_dir is not None else {}),
            )
        if browser_verification and is_captcha_page(response_body.decode("utf-8", errors="replace")):
            if effective_proxy:
                return result  # The headless worker must not open a direct browser.
            print(f"[登录验证] {result.error_msg}", flush=True)
            return await browser_login(account, password, char_id, login_url, register_url)
    return result
