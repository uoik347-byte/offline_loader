"""
socket_client.py — TCP Socket 连接 + 二进制协议收发
精确对照 H5 源码:
  SocketServerClient.ts:68-130    → connect + sendXtMessage + writeBinaryToSocket
  BinaryMsgWriter.ts:45-52,71-81 → login + sendXtMessage 内层格式
  BinaryMsgWriter.ts:180-185     → resetMsgBuffer 包头

协议层次:
  上行: [4B 大端: 包体长度+4] [4B 大端: 包序号] [XOR 加密的包体]
  下行: [4B 大端: 包体长度] [1B: 消息类型] [消息体]
  包体:
    [1B: 消息类型 (0=系统,1=扩展)]
    [2B 大端: action 命令号]
    [4B 大端: roomId (-1=全局)]
    [N bytes: 消息体 (登录=3个UTF串, 扩展=extId+cvt+AMF3)]
"""
from __future__ import annotations
import asyncio
import logging
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any

from src.config import UpCmd, DownCmd, MSG_TYPE_SYS, MSG_TYPE_EXT
from src.protocol.msgseq import MsgSeq, InnerSeq
from src.protocol.amf3 import Amf3Reader
from src.protocol.encrypt import outer_xor_encrypt
from src.protocol.message_codec import (
    build_login_message,
    build_message,
    build_xt_request,
    pack_int,
    pack_short,
    pack_utf,
    parse_legacy_fields,
)


# Backward-compatible aliases used by the retained debug scripts.
_pack_utf = pack_utf
_pack_short = pack_short
_pack_int = pack_int

logger = logging.getLogger(__name__)


# The repository client does not send <policy-file-request/>. It reads a
# server-pushed policy on the game socket, one byte at a time. This is the
# initial per-byte wait; a received policy that denies the port still stops.
PASSIVE_POLICY_BYTE_TIMEOUT = 2.0


def _policy_allows_game_socket(policy: bytes, port: int) -> bool:
    try:
        root = ET.fromstring(policy)
    except ET.ParseError:
        return False
    if root.tag != 'cross-domain-policy':
        return False
    origin = 'aola.100bt.com'
    for rule in root.findall('allow-access-from'):
        domain = rule.get('domain', '').lower()
        if not (domain in {'*', origin} or (domain.startswith('*.') and origin.endswith(domain[1:]))):
            continue
        for allowed in rule.get('to-ports', '').split(','):
            allowed = allowed.strip()
            if allowed == '*' or (allowed.isdigit() and int(allowed) == port):
                return True
            bounds = allowed.split('-')
            if len(bounds) == 2 and all(value.isdigit() for value in bounds):
                if int(bounds[0]) <= port <= int(bounds[1]):
                    return True
    return False


async def _read_nul_terminated(readexactly, timeout: float) -> bytes:
    policy = bytearray()
    async with asyncio.timeout(timeout):
        while True:
            chunk = await readexactly(1)
            if chunk[0] == 0:
                return bytes(policy)
            policy.append(chunk[0])
            if len(policy) > 65536:
                raise ValueError("安全策略过长")


def _safe_token(value: object, limit: int = 40) -> str:
    text = "" if value is None else str(value)
    if not text or len(text) > limit:
        return ""
    if not text.isascii():
        return ""
    if any(not (char.isalnum() or char in "_=.-") for char in text):
        return ""
    return text


def _safe_close_note(reason: str) -> str:
    text = (reason or "").replace("\n", " ").strip()
    if text.startswith("TCP EOF"):
        return text[:120]
    if text.startswith("TCP 收包失败:") or text.startswith("WSS 收包失败:") or text.startswith("发送失败:"):
        kind = text.split(":", 1)[0]
        tail = text.split(":", 1)[1].strip().split(":", 1)[0].strip()
        name = tail if _safe_token(tail, 40) else "异常"
        return f"{kind}: {name}"
    if text.startswith("WSS 已关闭:"):
        code = ""
        for part in text.replace(",", " ").split():
            if part.startswith("code="):
                code = part
                break
        return "WSS 已关闭" + (f" {code}" if _safe_token(code.removeprefix("code="), 8) else "")
    return ""


def summarize_login_reply(
    reply: dict | None,
    *,
    body_bytes: int,
    timed_out: bool,
    honor_count: int,
    parse_fail_count: int,
    parse_fail_bytes: int,
    parse_fail_type: int | None,
    closed_note: str,
    wait_seconds: float,
) -> tuple[str, bool]:
    """Classify one game-login observation without copying payload text or ids."""
    wait = f"{wait_seconds:g}"
    if isinstance(reply, dict) and reply:
        command = _safe_token(reply.get("_cmd")) or "无短命令名"
        action = reply.get("_action")
        action_text = str(action) if isinstance(action, int) and not isinstance(action, bool) else "无"
        if reply.get("_cmd") == "logKO":
            return (
                f"服务器拒绝游戏登录，命令=logKO，动作={action_text}，"
                f"正文{body_bytes}字节。不记录拒绝原文。",
                True,
            )
        return (
            f"收到登录回包但未接受：命令={command}，动作={action_text}，"
            f"正文{body_bytes}字节，没有正整数 id，也不是 logOK。",
            False,
        )
    if parse_fail_count:
        type_text = str(parse_fail_type) if parse_fail_type is not None else "无"
        return (
            f"登录等待{wait}秒。解析失败{parse_fail_count}次，"
            f"最后一包{parse_fail_bytes}字节，类型={type_text}。没有可接受的登录回包。",
            False,
        )
    if closed_note:
        if closed_note.startswith("TCP EOF"):
            return (
                f"登录收包时连接已关闭：{closed_note}。这不是等满{wait}秒。",
                False,
            )
        return (f"登录收包结束：{closed_note}。等待上限{wait}秒。", False)
    if honor_count:
        return (
            f"登录等待{wait}秒。只跳过{honor_count}条 honorMsgDispatch，没有登录回包。",
            False,
        )
    if timed_out:
        return (f"登录等待{wait}秒没有收到回包。", False)
    if body_bytes:
        return (f"收到{body_bytes}字节，但不是登录对象。", False)
    return ("游戏登录没有返回可接受的回包。", False)


def endpoint_facts(zone) -> str:
    domain = str(getattr(zone, "domain", "") or "").strip()
    host = str(getattr(zone, "host", "") or "").strip()
    if len(host) > 80:
        host = host[:80] + "…"
    if len(domain) > 80:
        domain = domain[:80] + "…"
    return (
        f"server_index={getattr(zone, 'server_index', -1)} "
        f"host={host or '空'} flash_port={getattr(zone, 'flash_port', 0)} "
        f"h5_port={getattr(zone, 'port', 0)} domain={domain or '空'}"
    )


class GameSocket:
    """
    游戏 TCP/WebSocket 客户端 — 完整协议栈
    """
    def __init__(self, session_id: str = "", proxy_url: str | None = None, *, network_mode: str = "legacy"):
        from src.network.proxy import process_proxy
        self.proxy_url = None if network_mode == "system" else (proxy_url or process_proxy())
        self._system_network = network_mode == "system"
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ws: Any = None  # websockets connection
        self._connected = False
        self._use_ws = False
        self.disconnect_reason = ""
        # A desktop batch and an interaction script can otherwise reach the
        # same account concurrently. Keep sequence generation and transport
        # writes atomic per connection while leaving different accounts free
        # to send in parallel.
        self._send_lock = asyncio.Lock()

        # 外层包序号
        self._packet_num = -1
        self._msg_seq = MsgSeq(session_id)

        # 内层消息序号
        self._inner_seq = InnerSeq()

        self.my_user_id = 0
        self.login_response: dict = {}
        self.last_login_summary = ""
        self.login_explicit_reject = False
        self.record_recv_diag = False
        self._parse_fail_count = 0
        self._parse_fail_bytes = 0
        self._parse_fail_type: int | None = None
        self.active_room_id = -1
        self.active_room_name = ""

    @property
    def connected(self) -> bool:
        return self._connected

    async def _open_tcp(self, host: str, port: int, timeout: float) -> None:
        if self.proxy_url:
            from src.network.proxy import proxy_socket
            sock = await proxy_socket(self.proxy_url, host, port, timeout)
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(sock=sock), timeout=timeout
                )
            except BaseException:
                sock.close()
                raise
        else:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        self._connected = True
        self._use_ws = False
        self.disconnect_reason = ""
        self.active_room_id = -1
        self.active_room_name = ""

    async def _read_pushed_policy(self) -> bytes | None:
        """Read a server-pushed policy. None means the first byte did not arrive."""
        assert self._reader is not None
        policy = bytearray()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self._reader.readexactly(1), PASSIVE_POLICY_BYTE_TIMEOUT
                )
            except asyncio.TimeoutError:
                if policy:
                    raise
                return None
            if chunk[0] == 0:
                return bytes(policy)
            policy.append(chunk[0])
            if len(policy) > 65536:
                raise ValueError("安全策略过长")

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float = 15.0,
        policy_timeout: float | None = None,
    ) -> bool:
        """Open one game socket and read a pushed policy before login.

        This client does not send a policy-file request and does not open a
        second socket. ``policy_timeout`` is retained for callers and is not a
        request deadline. A policy that arrives and does not allow the port
        still stops the connection.
        """
        del policy_timeout
        try:
            await self._open_tcp(host, port, timeout)
            try:
                policy = await self._read_pushed_policy()
            except asyncio.TimeoutError:
                self.disconnect_reason = "TCP 安全策略在空字节前中断，已停止连接"
                await self.close()
                print(f"[TCP] Connection failed: {self.disconnect_reason}")
                return False
            except ValueError:
                self.disconnect_reason = "TCP 安全策略无效或过长，已停止连接"
                await self.close()
                print(f"[TCP] Connection failed: {self.disconnect_reason}")
                return False
            if policy is None:
                print("[TCP] No pushed policy; game login stays on this socket")
                return True
            if not _policy_allows_game_socket(policy, port):
                self.disconnect_reason = "TCP 安全策略无效或未允许当前游戏域名及端口，已停止连接"
                await self.close()
                print(f"[TCP] Connection failed: {self.disconnect_reason}")
                return False
            print(f"[TCP] Pushed policy accepted for port {port}")
            return True
        except asyncio.IncompleteReadError as e:
            self.disconnect_reason = (
                f"TCP 安全策略读取时连接提前关闭：期望 {e.expected} 字节，"
                f"收到 {len(e.partial)} 字节"
            )
            await self.close()
            print(f"[TCP] Connection failed: {self.disconnect_reason}")
            return False
        except asyncio.CancelledError:
            await self.close()
            raise
        except (OSError, asyncio.TimeoutError) as e:
            self.disconnect_reason = f"TCP 连接失败: {type(e).__name__}: {e}"
            await self.close()
            print(f"[TCP] Connection failed: {e}")
            return False

    async def connect_ws(self, domain: str, port: int, timeout: float = 2) -> bool:
        """WebSocket 连接 (H5 端口)"""
        try:
            import websockets, ssl
            ws_url = f"wss://{domain}:{port}"
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connection_options = {"proxy": None} if self._system_network else {}
            sock = None
            if self.proxy_url:
                from src.network.proxy import proxy_socket
                sock = await proxy_socket(self.proxy_url, domain, port, timeout)
                connection_options = {"sock": sock, "proxy": None}
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(ws_url, ping_interval=None, ssl=ssl_ctx,
                                       additional_headers={"Origin": "https://aola.100bt.com"},
                                       user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                       **connection_options),
                    timeout=timeout
                )
            except BaseException:
                if sock is not None:
                    sock.close()
                raise
            self._connected = True
            self._use_ws = True
            self.disconnect_reason = ""
            self.active_room_id = -1
            self.active_room_name = ""
            print(f"[WSS] Connected to {ws_url}")
            return True
        except Exception as e:
            self.disconnect_reason = f"WSS 连接失败: {type(e).__name__}: {e}"
            print(f"[WSS] Connection failed: {e}")
            return False

    async def close(self) -> None:
        if not self.disconnect_reason:
            self.disconnect_reason = "客户端主动断开"
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
        self._reader = None
        self._writer = None
        self.active_room_id = -1
        self.active_room_name = ""

    # ── 发包 ──
    async def _send_raw(self, body: bytes) -> None:
        """发送二进制包 (TCP 或 WebSocket)"""
        async with self._send_lock:
            await self._send_raw_locked(body)

    async def _send_raw_locked(self, body: bytes) -> None:
        """Build and write one frame while the caller owns ``_send_lock``."""
        seq = self._gen_packet_num()
        action = struct.unpack(">H", body[1:3])[0] if len(body) >= 3 else -1
        logger.info(
            "TX frame: outer_seq=%d (0x%08X), action=%d, body_bytes=%d",
            seq,
            seq & 0xFFFFFFFF,
            action,
            len(body),
        )
        encrypted = outer_xor_encrypt(body, seq)
        header = (
            struct.pack('>I', len(encrypted) + 4)
            + struct.pack('>I', seq & 0xFFFFFFFF)
        )
        packet = header + encrypted

        try:
            if self._ws:
                await self._ws.send(packet)
            elif self._writer:
                self._writer.write(packet)
                await self._writer.drain()
            else:
                raise ConnectionError("没有可用的游戏连接")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected = False
            self.disconnect_reason = (
                f"发送失败: {type(exc).__name__}: {exc}"
            )
            logger.exception("game packet send failed")
            raise
    
    def _gen_packet_num(self) -> int:
        if self._packet_num == -1:
            self._packet_num = 0
        else:
            self._packet_num = self._msg_seq.next(
                self._packet_num,
                self.my_user_id if self.my_user_id > 0 else 0
            )
        return self._packet_num

    # ── 收包 ──
    async def _recv_packet(self) -> tuple[bytes | None, int | None]:
        """Receive one server frame body.

        Server frames are asymmetric with client frames: their length prefix
        is followed immediately by the message type.  They do not contain an
        outer sequence and are not XOR encrypted.
        """
        import asyncio as _a
        if self._ws:
            from websockets.exceptions import ConnectionClosed

            try:
                payload = await _a.wait_for(self._ws.recv(), timeout=30.0)
                if isinstance(payload, str):
                    raise ValueError("Expected binary")
            except _a.TimeoutError:
                return (None, None)
            except ValueError:
                logger.warning("ignored non-binary WSS message")
                return (None, None)
            except ConnectionClosed as exc:
                self.disconnect_reason = (
                    f"WSS 已关闭: code={exc.code}, reason={exc.reason or '-'}"
                )
                logger.warning(self.disconnect_reason)
                self._connected = False
                self.active_room_id = -1
                self.active_room_name = ""
                return (None, None)
            except Exception as exc:
                self.disconnect_reason = (
                    f"WSS 收包失败: {type(exc).__name__}: {exc}"
                )
                logger.exception("WSS receive failed")
                self._connected = False
                self.active_room_id = -1
                self.active_room_name = ""
                return (None, None)
            if not isinstance(payload, (bytes, bytearray)) or len(payload) < 5:
                raise ValueError("服务端 WSS 帧短于长度和类型字段")
            payload = bytes(payload)
            declared_length = struct.unpack(">I", payload[:4])[0]
            actual_length = len(payload) - 4
            if declared_length != actual_length:
                raise ValueError(
                    "服务端 WSS 帧长度不匹配："
                    f"declared={declared_length}, actual={actual_length}"
                )
            return (payload[4:], None)
        elif self._reader:
            try:
                len_bytes = await self._reader.readexactly(4)
                outer_len = struct.unpack('>I', len_bytes)[0]
                payload = await self._reader.readexactly(outer_len)
            except asyncio.IncompleteReadError as exc:
                self.disconnect_reason = (
                    f"TCP EOF: expected={exc.expected}, received={len(exc.partial)}"
                )
                logger.warning(self.disconnect_reason)
                self._connected = False
                self.active_room_id = -1
                self.active_room_name = ""
                return (None, None)
            except OSError as exc:
                self.disconnect_reason = (
                    f"TCP 收包失败: {type(exc).__name__}: {exc}"
                )
                logger.exception("TCP receive failed")
                self._connected = False
                self.active_room_id = -1
                self.active_room_name = ""
                return (None, None)
            if len(payload) < 1:
                return (None, None)
            return (payload, None)
        return (None, None)

    def _decode_server_body(self, body: bytes) -> dict:
        """Decode one validated server body beginning with its message type."""
        if not body:
            raise ValueError("服务端消息缺少类型字段")

        msg_type = body[0]
        if msg_type == MSG_TYPE_SYS:
            system = self._parse_headered_system(body)
            if system is None:
                if len(body) < 7:
                    raise ValueError("服务端系统消息头不完整")
                action = struct.unpack(">H", body[1:3])[0]
                room_id = struct.unpack(">i", body[3:7])[0]
                fields = self._parse_sys_fields(body[7:])
                system = {"_type": MSG_TYPE_SYS, "_action": action, "_room_id": room_id}
                if fields:
                    system.update(fields)
            if system.get("_cmd") == "joinOK":
                room_id = system.get("room_id")
                header_room_id = system.get("_room_id")
                valid_room_id = (
                    isinstance(room_id, int)
                    and not isinstance(room_id, bool)
                    and room_id > 0
                )
                valid_header = header_room_id == -1 or (
                    isinstance(header_room_id, int)
                    and not isinstance(header_room_id, bool)
                    and header_room_id > 0
                    and header_room_id == room_id
                )
                if valid_room_id and valid_header:
                    self.active_room_id = room_id
                    self.active_room_name = str(system.get("room_name", ""))
                else:
                    self.active_room_id = -1
                    self.active_room_name = ""
                    system["parse_error"] = (
                        "joinOK 房间 ID 无效或不一致："
                        f"header={header_room_id!r}, payload={room_id!r}"
                    )
            elif system.get("_cmd") in {"joinKO", "logKO"}:
                self.active_room_id = -1
                self.active_room_name = ""
            return system

        if msg_type == MSG_TYPE_EXT:
            try:
                message = Amf3Reader(body[1:]).read_object()
            except (EOFError, ValueError, IndexError, struct.error) as exc:
                raise ValueError(
                    f"服务端 AMF3 消息解析失败：type={msg_type}, bytes={len(body)}"
                ) from exc
            if not isinstance(message, dict):
                raise ValueError(
                    "服务端扩展消息不是 AMF3 对象："
                    f"type={msg_type}, bytes={len(body)}"
                )
            return message

        if msg_type == 2:
            return {"_type": 2, "data": body[1:]}

        raise ValueError(
            f"未知服务端消息类型：type={msg_type}, bytes={len(body)}"
        )

    async def _recv_any_packet(self, timeout: float = 15.0) -> tuple[dict | None, bytes | None]:
        """接收下一个消息并解析"""
        import asyncio
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0: return (None, None)
            try:
                result = await asyncio.wait_for(self._recv_packet(), timeout=remaining)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return (None, None)
            if result is None or result[0] is None: return (None, None)
            body_raw, _sequence = result
            if not isinstance(body_raw, (bytes, bytearray)) or len(body_raw) < 1:
                continue
            try:
                message = self._decode_server_body(bytes(body_raw))
            except (ValueError, EOFError, IndexError, struct.error) as exc:
                if self.record_recv_diag:
                    self._parse_fail_count += 1
                    self._parse_fail_bytes = len(body_raw)
                    self._parse_fail_type = body_raw[0]
                logger.warning(
                    "server packet parse failed: bytes=%d, type=%d, error=%s",
                    len(body_raw),
                    body_raw[0],
                    type(exc).__name__,
                )
                continue
            return (message, bytes(body_raw))

    def _parse_sys_fields(self, data: bytes) -> dict:
        return parse_legacy_fields(data)

    @staticmethod
    def _read_prefix_string(data: bytes, position: int) -> tuple[str, int]:
        if position + 2 > len(data):
            raise ValueError("系统消息缺少字符串长度")
        length = struct.unpack(">H", data[position:position + 2])[0]
        start = position + 2
        end = start + length
        if end > len(data):
            raise ValueError("系统消息字符串超出数据范围")
        return data[start:end].decode("utf-8", errors="replace"), end

    def _parse_headered_system(self, data: bytes) -> dict | None:
        """Parse the unencrypted SYS room/login error messages used by H5."""
        if len(data) < 7 or data[0] != MSG_TYPE_SYS:
            return None
        action = struct.unpack(">H", data[1:3])[0]
        if action not in {DownCmd.joinOK, DownCmd.joinKO, DownCmd.logKO}:
            return None
        header_room_id = struct.unpack(">i", data[3:7])[0]
        result = {"_action": action, "_room_id": header_room_id}
        position = 7
        try:
            if action == DownCmd.joinOK:
                if position + 4 > len(data):
                    raise ValueError("joinOK 缺少房间 ID")
                room_id = struct.unpack(">i", data[position:position + 4])[0]
                position += 4
                room_name, position = self._read_prefix_string(data, position)
                if position + 4 > len(data):
                    raise ValueError("joinOK 缺少房间容量")
                max_users = struct.unpack(">i", data[position:position + 4])[0]
                position += 4
                join_data, _ = self._read_prefix_string(data, position)
                result.update(
                    {
                        "_cmd": "joinOK",
                        "room_id": room_id,
                        "room_name": room_name,
                        "max_users": max_users,
                        "join_data": join_data,
                    }
                )
            elif action == DownCmd.joinKO:
                error, _ = self._read_prefix_string(data, position)
                result.update({"_cmd": "joinKO", "msg": error})
            else:
                error, _ = self._read_prefix_string(data, position)
                result.update({"_cmd": "logKO", "msg": error})
        except ValueError as exc:
            command = {
                DownCmd.joinOK: "joinOK",
                DownCmd.joinKO: "joinKO",
                DownCmd.logKO: "logKO",
            }[action]
            result.update({"_cmd": command, "parse_error": str(exc)})
        return result

    def _parse_sys_fields_legacy(self, data: bytes) -> dict:
        """解析老的 AMF 字段格式: 先读所有字段名再读值"""
        p = 0
        names = []

        # Phase 1: 读取名字。name_len 为奇数(2*strlen+1)，值类型为 1-6
        # 策略: 贪心读名字直到 name_len < 2（无效长度），且确保至少读了所有可解析的名字
        while p + 1 < len(data):
            field_start = p
            name_len = data[p]
            # 如果字节是有效的值类型(1-6)且不是合法的 name_len(必须是奇数>=3)，切换
            # 1,2 是类型(不是name_len); 4,6 是类型(偶数→不是name_len)
            # 3,5 既是类型也是name_len → 二义性时先当name_len读
            if name_len < 3 or name_len > 200 or name_len % 2 == 0:
                break
            p += 1
            name_bytes = (name_len - 1) // 2
            if name_bytes <= 0 or p + name_bytes > len(data):
                # 回退
                p -= 1
                break
            try:
                name = data[p:p + name_bytes].decode('utf-8', errors='replace')
            except:
                break
            p += name_len // 2
            if name and name.isprintable() and len(name) > 0:
                names.append(name)
            else:
                p = field_start
                break
        
        if not names:
            # Fallback: return empty
            return {}

        # Phase 2: values are consecutive AMF3 values sharing reference tables.
        fields = {}
        reader = Amf3Reader(data[p:])
        for name in names:
            try:
                fields[name] = reader.read_object()
            except (EOFError, ValueError, IndexError, struct.error):
                break
        
        return fields

    # ── 构建内层包体 ──

    def _build_packet(self, msg_type: int, action: int, room_id: int, body: bytes) -> bytes:
        """BinaryMsgWriter.ts:180-185 — resetMsgBuffer + body"""
        return build_message(msg_type, action, room_id, body)

    # ── 登录 ──

    async def login(self, zone: str, user_id: str, session_id: str) -> dict | None:
        """
        发送登录包并接收响应
        """
        packet = build_login_message(zone, user_id, session_id)
        print(f"[LOGIN] Sending packet: {len(packet)} bytes, zone_bytes={len(zone.encode('utf-8'))}")
        await self._send_raw(packet)
        self.login_explicit_reject = False
        self._parse_fail_count = 0
        self._parse_fail_bytes = 0
        self._parse_fail_type = None
        self.record_recv_diag = True
        honor_count = 0
        wait_seconds = 15.0
        resp, raw = None, None
        try:
            deadline = asyncio.get_running_loop().time() + wait_seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    resp, raw = None, None
                    break
                resp, raw = await self._recv_any_packet(timeout=remaining)
                if not isinstance(resp, dict) or resp.get("_cmd") != "honorMsgDispatch":
                    break
                honor_count += 1
        finally:
            self.record_recv_diag = False
        summary, explicit = summarize_login_reply(
            resp if isinstance(resp, dict) else None,
            body_bytes=len(raw) if isinstance(raw, (bytes, bytearray)) else 0,
            timed_out=resp is None and raw is None and not self.disconnect_reason,
            honor_count=honor_count,
            parse_fail_count=self._parse_fail_count,
            parse_fail_bytes=self._parse_fail_bytes,
            parse_fail_type=self._parse_fail_type,
            closed_note=_safe_close_note(self.disconnect_reason),
            wait_seconds=wait_seconds,
        )
        self.last_login_summary = summary
        self.login_explicit_reject = explicit
        if not isinstance(resp, dict) or not resp:
            return None
        self.login_response = dict(resp)
        uid = resp.get("id")
        if uid is not None and not isinstance(uid, bool):
            try:
                self.my_user_id = uid if isinstance(uid, int) else int(uid)
            except (TypeError, ValueError):
                self.my_user_id = 0
        if self.my_user_id > 0 or resp.get("_cmd") == "logOK":
            self.login_explicit_reject = False
            return resp
        return None
    # ── 发送扩展消息 ──

    async def send_xt_message(
        self, ext_id: int, cmd: str, params: dict,
        room_id: int = -1, val: int = 0, encrypt: bool = True
    ) -> None:
        """
        BinaryMsgWriter.ts:71-81 — sendXtMessage
        
        包格式: [type=1][action=10033][roomId][2B extId][UTF cmd][AMF3 {data: ByteArray}]
        
        内层 encrypt (BinaryMsgWriter.ts:82-103):
          1. getMessageSequence(extId) → seq
          2. params[":ext_seq;"] = seq
          3. AMF3 序列化 params → byte array
          4. encryptByteArray(seq, amf3_bytes) → 加密
          5. 包裹为 AMF3 对象: { "data": ByteArray(encrypted) }
        """
        async with self._send_lock:
            effective_room_id = self.active_room_id if room_id == -1 else room_id
            # Generate both sequence layers and write their frame as one
            # per-account operation so concurrent callers cannot interleave.
            seq = (
                val
                if val > 0
                else self._inner_seq.get_message_sequence(
                    ext_id, self.my_user_id
                )
            )

            logger.info(
                "EXT encode: ext_id=%d, cmd=%s, room_id=%d, inner_seq=%d (0x%08X), inner_key=%d",
                ext_id,
                cmd,
                effective_room_id,
                seq,
                seq & 0xFFFFFFFF,
                (seq & 0x1FFFFFFC) >> 2,
            )

            inner = build_xt_request(
                ext_id, cmd, params, seq, effective_room_id, encrypt
            )
            await self._send_raw_locked(inner)

    async def send_and_recv(self, ext_id: int, cmd: str, params: dict,
                            room_id: int = -1, timeout: float = 10.0) -> dict | None:
        """发送 EXT 命令并等待响应"""
        await self.send_xt_message(ext_id, cmd, params, room_id)
        # 服务端 EXT 响应从 type 后直接承载 AMF3 对象。
        result, _ = await self._recv_any_packet(timeout=timeout)
        return result

    async def recv_message(self, timeout: float = 15.0) -> dict | None:
        """Receive and decode one message from the connection."""
        result, _ = await self._recv_any_packet(timeout=timeout)
        return result

    async def recv_ext_responses(self, timeout: float = 3.0, max_count: int = 100) -> list[dict]:
        """Continuously receive and decode responses until timeout."""
        results = []
        import asyncio
        deadline = asyncio.get_event_loop().time() + timeout
        while len(results) < max_count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            result, _ = await self._recv_any_packet(timeout=remaining)
            if result is None:
                break
            results.append(result)
        return results

    async def _recv_ext_responses_legacy(self, timeout: float = 3.0, max_count: int = 100) -> list[dict]:
        """
        接收多个 EXT 响应，直到超时或达到上限。
        用于持续监听服务端推送的 EXT 数据。
        返回解析后的 dict 列表。
        """
        results = []
        import asyncio
        deadline = asyncio.get_event_loop().time() + timeout
        while len(results) < max_count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                result = await asyncio.wait_for(self._recv_packet(), timeout=remaining)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                break
            if result is None or result[0] is None:
                break
            body_raw, seq = result
            if not isinstance(body_raw, (bytes, bytearray)) or len(body_raw) < 1:
                continue

            # EXT 响应: body 直接是 AMF3 数据 (无 [type][action] 包头)
            # 第一字节是 AMF3 类型, 直接解析
            if first_byte == MSG_TYPE_EXT:
                body = outer_xor_encrypt(body_raw, seq)
                if len(body) < 1:
                    continue
                try:
                    amf_reader = Amf3Reader(body)
                    amf_obj = amf_reader.read_object()
                except (EOFError, ValueError):
                    amf_obj = {}
                if isinstance(amf_obj, dict):
                    results.append(amf_obj)
                continue
            # SYS 消息: body[pos:] 是老格式字段
            if len(body) > 1:
                result_dict = self._parse_sys_fields(body)
                if result_dict:
                    results.append(result_dict)
            # Skip non-EXT messages (SYS, etc.)
        return results



async def _connect_tcp_with_retry(sock, host, port, timeout, window):
    deadline = time.monotonic() + window
    attempt = 0
    while (remaining := deadline - time.monotonic()) > 0:
        attempt += 1
        print(f"[TCP] Attempt {attempt}, remaining={remaining:.3f}s")
        try:
            async with asyncio.timeout(remaining):
                connected = await sock.connect_tcp(host, port, min(timeout, remaining))
        except TimeoutError:
            sock.disconnect_reason = "TCP 连接重试窗口已结束"
            connected = False
        if connected:
            return True
        await sock.close()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(0.5, remaining))
    return False


def _remember_stage(stage_notes: list[str] | None, text: str) -> None:
    if stage_notes is None:
        return
    cleaned = text.replace("\n", " ").strip()
    lowered = cleaned.lower()
    for marker in ("password", "session_id", "session", "cookie", "token", "sid"):
        start = lowered.find(marker)
        if start >= 0:
            cleaned = cleaned[:start] + "[REDACTED]"
            break
    if len(cleaned) > 180:
        cleaned = cleaned[:180] + "…"
    stage_notes.append(cleaned)


async def login_and_connect(
    zone, user_id: str, session_id: str,
    timeout: float = 15.0,
    proxy_url: str | None = None,
    network_mode: str = "legacy",
    tcp_retry_window: float | None = None,
    stage_notes: list[str] | None = None,
    policy_timeout: float | None = None,
) -> GameSocket | None:
    """TCP/WSS login; a retry window selects TCP only and retries before sending login.

    The socket login packet is zone, user id and session id. It does not resend charId.
    ``stage_notes`` receives short stage results and must stay free of credentials.
    """
    if network_mode == "system":
        sock = GameSocket(session_id, network_mode="system")
    else:
        sock = GameSocket(session_id, proxy_url=proxy_url) if proxy_url else GameSocket(session_id)
    retained = False
    zone_label = f"{zone.zone_index} {zone.zone_name}"
    try:
        # 策略 2: 原始 TCP (Flash port)。flash 端口为 0 时才退到 H5 端口。
        tcp_port = zone.flash_port or zone.port
        if not zone.host or tcp_port <= 0:
            connected = False
            _remember_stage(stage_notes, "Flash TCP 未尝试：区服没有 host 或端口")
        else:
            connected = (
                await _connect_tcp_with_retry(sock, zone.host, tcp_port, timeout, tcp_retry_window)
                if tcp_retry_window is not None
                else await sock.connect_tcp(
                    zone.host, tcp_port, timeout, policy_timeout=policy_timeout
                )
            )
            if not connected:
                _remember_stage(
                    stage_notes,
                    "Flash TCP 未连上："
                    + (sock.disconnect_reason or "无断开说明")
                    + "。"
                    + endpoint_facts(zone),
                )
        if connected:
            resp = await sock.login(zone_label, user_id, session_id)
            accepted = resp is not None and (
                tcp_retry_window is None or resp.get("_cmd") == "logOK"
            )
            if accepted:
                retained = True
                _remember_stage(stage_notes, "Flash TCP 游戏登录已接受")
                return sock
            if resp is None:
                _remember_stage(
                    stage_notes,
                    "Flash TCP 已连上，" + (sock.last_login_summary or "游戏登录没有返回可接受的回包"),
                )
                if sock.login_explicit_reject:
                    _remember_stage(stage_notes, "已收到明确拒绝，不再尝试 H5 WebSocket")
                    return None
            else:
                command = str(resp.get("_cmd", ""))
                _remember_stage(
                    stage_notes,
                    f"Flash TCP 已连上，回包命令不是 logOK（cmd={command}），这条连接已放弃",
                )
            await sock.close()

        if tcp_retry_window is not None:
            _remember_stage(stage_notes, "已跳过 H5 WebSocket：本次指定了只重试 Flash TCP")
            return None

        # 策略 1: WebSocket (H5 domain:port)
        if not (zone.domain and zone.port > 0):
            _remember_stage(
                stage_notes,
                "H5 WebSocket 未尝试：解析结果没有同时具备 domain 和 h5 端口。"
                + endpoint_facts(zone),
            )
            return None
        if not await sock.connect_ws(zone.domain, zone.port, timeout):
            _remember_stage(
                stage_notes,
                "H5 WebSocket 未连上：" + (sock.disconnect_reason or "无断开说明"),
            )
            return None
        resp = await sock.login(zone_label, user_id, session_id)
        if resp is not None:
            retained = True
            _remember_stage(stage_notes, "H5 WebSocket 游戏登录已接受")
            return sock
        _remember_stage(
            stage_notes,
            "H5 WebSocket 已连上，" + (sock.last_login_summary or "游戏登录没有返回可接受的回包"),
        )
        return None
    finally:
        if not retained:
            await sock.close()
