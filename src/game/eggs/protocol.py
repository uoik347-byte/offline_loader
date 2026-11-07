"""Egg commands and reply parsers taken from the repo hatch script.

Evidence in ``scripts/auto_hatch.py`` and the attached hatch notes:

- ext 17, cmd ``1-8``, param ``{}``: hatch status. Reply ``_cmd=1-8`` and
  ``es`` items use ``ei`` (egg id), ``pmid`` (collect id) and ``st``
  (100 means ready to collect).
- ext 17, cmd ``1=1``, param ``{}``: unhatched eggs. Reply items use ``ei``.
- ext 17, cmd ``1-4``, param ``{"eggId", "id"}``: start hatching into slot
  ``id`` 1, 2 or 3. A matching echo repeats ``eggId`` and ``id``.
- ``MessageBox_2`` with ``s=-1`` and ``id=14`` is the documented invalid-egg
  notice. It has no egg id, so it cannot by itself prove which egg failed.
- ext 13, cmd ``1223``, param ``{"loc": 4, "id": pmid}``. The script waits
  for ``_cmd=2223`` and reads ``id``. No other 2223 field is documented.

A missing or non-list ``es`` is malformed. It is not an empty egg list.
"""
from __future__ import annotations

from dataclasses import dataclass


EXT_HATCH = 17
CMD_STATUS = "1-8"
CMD_UNHATCHED = "1=1"
CMD_HATCH = "1-4"
EXT_COLLECT = 13
CMD_COLLECT = "1223"
CMD_COLLECT_ACK = "2223"
COLLECT_LOC = 4
CMD_MESSAGE_BOX = "MessageBox_2"
INVALID_EGG_BOX = (-1, 14)
SLOT_IDS = (1, 2, 3)
READY_ST = 100

# Reply waits already used by the hatch script. The old 1.1s sleep is not a
# completion signal and is not used. Status polls stay at or above 0.5s.
QUERY_TIMEOUT = 2.0
ECHO_TIMEOUT = 5.0
COLLECT_TIMEOUT = 0.8
MESSAGE_BOX_TIMEOUT = 0.6
POLL_INTERVAL = 1.0
READINESS_LIMIT = 60.0

_LOG_SEND_KEYS = ("eggId", "id", "loc")
_LOG_RECV_KEYS = ("ei", "eggId", "pmid", "st", "id", "s")


class QueryFailure(ValueError):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


@dataclass(frozen=True)
class Limits:
    query_timeout: float = QUERY_TIMEOUT
    echo_timeout: float = ECHO_TIMEOUT
    collect_timeout: float = COLLECT_TIMEOUT
    message_box_timeout: float = MESSAGE_BOX_TIMEOUT
    poll_interval: float = POLL_INTERVAL
    readiness_limit: float = READINESS_LIMIT

    def __post_init__(self) -> None:
        if self.poll_interval < 0.5:
            raise ValueError("状态轮询间隔不能小于 0.5 秒")
        if self.readiness_limit <= 0:
            raise ValueError("就绪等待上限必须大于 0")
        if min(self.query_timeout, self.echo_timeout, self.collect_timeout) < 0:
            raise ValueError("回包等待时间不能是负数")


@dataclass(frozen=True)
class PendingEgg:
    egg_id: int
    pmid: int
    st: int | None


def as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _require_cmd(message: dict, expected: str) -> None:
    if str(message.get("_cmd", "")) != expected:
        raise QueryFailure("malformed", f"回包 _cmd 不是 {expected}，不能当成空列表")


def parse_unhatched(message: dict) -> list[int]:
    _require_cmd(message, CMD_UNHATCHED)
    eggs = message.get("es")
    if not isinstance(eggs, list):
        raise QueryFailure("malformed", "1=1 回包没有 es 列表，不能当成空列表")
    egg_ids: list[int] = []
    seen: set[int] = set()
    for index, item in enumerate(eggs):
        if not isinstance(item, dict):
            raise QueryFailure("malformed", f"1=1 的 es[{index}] 不是对象")
        egg_id = as_int(item.get("ei"))
        if egg_id is None or egg_id <= 0:
            raise QueryFailure("malformed", f"1=1 的 es[{index}] 缺少有效 ei")
        if egg_id in seen:
            raise QueryFailure("malformed", f"1=1 出现重复 eggId={egg_id}")
        seen.add(egg_id)
        egg_ids.append(egg_id)
    return egg_ids


def parse_status(message: dict) -> list[PendingEgg]:
    _require_cmd(message, CMD_STATUS)
    eggs = message.get("es")
    if not isinstance(eggs, list):
        raise QueryFailure("malformed", "1-8 回包没有 es 列表，不能当成空列表")
    pending: list[PendingEgg] = []
    seen: set[int] = set()
    for index, item in enumerate(eggs):
        if not isinstance(item, dict):
            raise QueryFailure("malformed", f"1-8 的 es[{index}] 不是对象")
        egg_id = as_int(item.get("ei"))
        pmid = as_int(item.get("pmid"))
        if egg_id is None or egg_id <= 0 or pmid is None or pmid <= 0:
            raise QueryFailure(
                "malformed", f"1-8 的 es[{index}] 缺少有效 ei 或 pmid"
            )
        if egg_id in seen:
            raise QueryFailure("malformed", f"1-8 出现重复 eggId={egg_id}")
        seen.add(egg_id)
        status = as_int(item.get("st")) if "st" in item else None
        pending.append(PendingEgg(egg_id, pmid, status))
    return pending


def match_cmd(expected: str):
    def _match(message: dict) -> bool:
        return str(message.get("_cmd", "")) == expected

    return _match


def match_hatch_result(egg_id: int, slot_id: int):
    def _match(message: dict) -> bool:
        return is_hatch_echo(message, egg_id, slot_id) or is_invalid_egg_box(message)

    return _match


def is_hatch_echo(message: dict, egg_id: int, slot_id: int) -> bool:
    return (
        str(message.get("_cmd", "")) == CMD_HATCH
        and as_int(message.get("eggId")) == egg_id
        and as_int(message.get("id")) == slot_id
    )


def is_invalid_egg_box(message: dict) -> bool:
    return (
        str(message.get("_cmd", "")) == CMD_MESSAGE_BOX
        and as_int(message.get("s")) == INVALID_EGG_BOX[0]
        and as_int(message.get("id")) == INVALID_EGG_BOX[1]
    )


def match_collect_ack(pmid: int):
    def _match(message: dict) -> bool:
        return (
            str(message.get("_cmd", "")) == CMD_COLLECT_ACK
            and as_int(message.get("id")) == pmid
            and pmid > 0
        )

    return _match


def format_send(ext_id: int, cmd: str, param: dict) -> str:
    parts = [f"发送 ext={ext_id} cmd={cmd}"]
    for key in _LOG_SEND_KEYS:
        if key in param and not isinstance(param[key], (dict, list)):
            parts.append(f"{key}={param[key]}")
    if set(param) - set(_LOG_SEND_KEYS):
        parts.append("其余参数已省略")
    return " ".join(parts)


def format_recv(message: dict) -> str:
    parts = [f"收到 cmd={message.get('_cmd', '?')}"]
    for key in _LOG_RECV_KEYS:
        value = message.get(key)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            parts.append(f"{key}={value}")
    if "es" in message:
        parts.append(f"es条数={_safe_count(message)}")
    parts.append("原始内容已省略")
    return " ".join(parts)


def _safe_count(message: dict) -> str:
    try:
        cmd = str(message.get("_cmd", ""))
        if cmd == CMD_UNHATCHED:
            return str(len(parse_unhatched(message)))
        if cmd == CMD_STATUS:
            return str(len(parse_status(message)))
    except QueryFailure:
        return "格式无效"
    return "未解析"


def public_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ")
    for marker in ("password", "session_id", "session", "cookie", "token", "sid"):
        lowered = text.lower()
        start = lowered.find(marker)
        if start >= 0:
            text = text[:start] + "[REDACTED]"
    return text[:300] or exc.__class__.__name__
