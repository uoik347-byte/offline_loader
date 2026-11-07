"""Plain Chinese for the end-user window. This module does not send commands."""
from __future__ import annotations

from src.game.eggs.engine import QueryView
from src.game.eggs.protocol import READY_ST, SLOT_IDS, public_error
from src.game.eggs.store import HoldStore

_HIDDEN = (
    "eggId",
    "pmid",
    "ext=",
    "cmd=",
    "1-8",
    "1=1",
    "1-4",
    "1223",
    "2223",
    "Traceback",
    "audit_absent",
)


def visible_failure(exc: BaseException) -> str:
    """Keep the failure, without protocol fields or a stack trace."""
    text = public_error(exc)
    if not text or any(marker in text for marker in _HIDDEN):
        return "操作没有完成。当前状态未知，没有把它当成空的，也没有记为成功。"
    return text


def start_confirmation(query: QueryView) -> str:
    waiting = len(query.unhatched or [])
    return (
        "开始后会自动孵化，并收取已经孵好的蛋。\n"
        "程序一次只用一个空着的孵化位，不会三个同时进行。\n"
        f"上次查询还有 {waiting} 个蛋没开始孵化。这不是实时数量。\n"
        "停止后不再开始新的孵化或收取。已经发出的无法撤回。\n"
        "重新打开程序不会自动继续。\n"
        "确定开始吗？"
    )


def collect_confirmation() -> str:
    return (
        "收取会先看一遍孵化器，只取已经孵好的蛋。\n"
        "不会开始新的孵化，也不会等还没孵好的蛋。\n"
        "如果这次没看清孵化器，就不会收取。\n"
        "确定收取吗？"
    )


def progress_text(confirmed: int, unconfirmed: int, query: QueryView | None) -> str:
    lines = [
        f"本次已确认收取 {confirmed} 个。",
        f"回执未确认 {unconfirmed} 个，不计为成功。",
    ]
    if query is None or query.unhatched is None:
        lines.append("还没开始孵化的数量未知。")
    else:
        lines.append(f"上次查询还有 {len(query.unhatched)} 个没开始孵化。这不是实时数量。")
    return "\n".join(lines)


def slot_text(store: HoldStore, *, fresh: bool, pending) -> str:
    """Three places, filled only when this program itself put an egg there."""
    by_slot = {}
    owned = set()
    for record in store.snapshot():
        slot = record.get("slot")
        if record.get("hatch_sent") and slot in SLOT_IDS and record.get("phase") != "audit_absent":
            by_slot[slot] = record
            owned.add(record["egg_id"])
    listed = {egg.egg_id: egg for egg in pending or []}
    lines = ["孵化位：一次只用一个空位"]
    if not fresh:
        lines.append("还不知道孵化器里有什么，下面不表示是空的。")
    for slot in SLOT_IDS:
        record = by_slot.get(slot)
        if record is None:
            lines.append(f"位置 {slot}：未知" if not fresh else f"位置 {slot}：空着")
            continue
        egg = listed.get(record["egg_id"])
        status = egg.st if egg is not None else record.get("st")
        phase = record.get("phase")
        if phase in {"hatch_uncertain", "collect_uncertain"} or record.get("collect_blocked"):
            state = "结果还不确定"
        elif status == READY_ST:
            state = "已经孵好"
        elif phase == "hatching":
            state = "正在孵化"
        else:
            state = "占用中"
        lines.append(f"位置 {slot}：{state}")
    if fresh:
        unknown = [egg.egg_id for egg in pending or [] if egg.egg_id not in owned]
        if unknown:
            lines.append(f"还有 {len(unknown)} 个蛋在孵化器里，但不知道在哪个位置。")
        if not pending and any(
            record.get("collect_blocked") or record.get("phase") in {"hatch_uncertain", "collect_uncertain"}
            for record in store.snapshot()
        ):
            lines.append("服务器上看着是空的，但本机还有未确认的操作，不能当成全部空了。")
    return "\n".join(lines)


def run_status(kind: str, confirmed: int, unconfirmed: int) -> str:
    if kind == "stopped":
        return (
            f"已停止。本次已确认收取 {confirmed} 个。"
            f"回执未确认 {unconfirmed} 个，不计为成功。"
        )
    if kind == "confirmation":
        return "已取消，没有开始。"
    if kind == "timeout":
        return "暂时没看清孵化器，没有继续。这不是空的。"
    if kind == "uncertain":
        return "结果还不确定，已暂停。没有重复发送，也没有记为成功。"
    if kind == "disconnected":
        return "连接已断开。没有继续。"
    if kind == "collected":
        return f"收取结束。本次已确认收取 {confirmed} 个。回执未确认 {unconfirmed} 个，不计为成功。"
    if kind == "idle":
        return (
            f"这次没有继续发送。本次已确认收取 {confirmed} 个。"
            f"回执未确认 {unconfirmed} 个，不计为成功。"
        )
    if kind == "held":
        return "孵化器里还有对不上位置的蛋，没有再放新的。"
    if unconfirmed:
        return f"回执未确认 {unconfirmed} 个，不计为成功。本次已确认收取 {confirmed} 个。"
    return f"操作结束。本次已确认收取 {confirmed} 个。"
