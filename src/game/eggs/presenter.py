"""Chinese text for the egg window. This module does not send commands."""
from __future__ import annotations

from src.game.eggs.engine import QueryView
from src.game.eggs.panel import IncubatorView, format_snapshot
from src.game.eggs.store import HoldStore


def can_start(connected: bool, query: QueryView | None, store_ok: bool, running: bool) -> bool:
    return bool(connected and store_ok and query is not None and query.ok and not running)


def confirmation_text(query: QueryView, store: HoldStore, retry_uncertain_collect: bool) -> str:
    pending = len(query.pending or [])
    unhatched = len(query.unhatched or [])
    held = len(store.snapshot())
    retry = "会" if retry_uncertain_collect else "不会"
    return (
        "开始后会向当前已连接的游戏服务器发送孵化（1-4）和收取（1223）。\n"
        f"最近一次查询：待处理 {pending} 个，未孵化 {unhatched} 个，本机占用记录 {held} 条。\n"
        "点“停止并收取”后马上不再孵化。已经可收取的蛋会收取；"
        "已经提交但还没就绪的蛋，只在现有时限内等待就绪后再收取。\n"
        "只有新的 1-8 确认是空的，才会显示格子已空。"
        "关闭窗口或断开会立刻停下，蛋留在游戏里，重新打开不会自动继续。\n"
        f"这次{retry}重试上次结果不确定的收取。已经提交过的孵化不会重发。\n"
        "重新打开程序不会自动继续。\n"
        "确定开始吗？"
    )


def format_board(
    query: QueryView | None,
    store: HoldStore,
    snapshot: IncubatorView | None = None,
) -> str:
    lines: list[str] = []
    if snapshot is None:
        lines.append("孵化格：空闲")
        lines.append("尚未成功刷新服务器列表。这不是空列表。")
        lines.append("协议没有剩余时间，这里不显示倒计时。")
    else:
        lines.append(format_snapshot(snapshot))
    lines.append("")
    if query is None:
        lines.append("还没有查询。打开程序后保持空闲。")
    elif not query.ok:
        lines.append(f"查询未完成：{query.detail}")
        if query.pending is None:
            lines.append("待收取列表未知。")
        else:
            lines.extend(_pending_lines(query.pending))
        if query.unhatched is None:
            lines.append("未孵化列表未知。")
        else:
            lines.append(_unhatched_line(query.unhatched))
    else:
        lines.append(query.detail)
        lines.extend(_pending_lines(query.pending or []))
        lines.append(_unhatched_line(query.unhatched or []))
    lines.append("本机占用记录：")
    held = store.snapshot()
    if not held:
        lines.append("无")
    for record in held:
        slot = record.get("slot")
        slot_text = f"格 {slot}" if slot else "格子未知"
        lines.append(
            f"eggId={record['egg_id']} {slot_text} pmid={record.get('pmid') or '-'} "
            f"st={record.get('st') if record.get('st') is not None else '-'} "
            f"{_phase_text(record.get('phase'))}"
        )
    if not store.ok:
        lines.append(store.error)
    return "\n".join(lines)


def _pending_lines(pending) -> list[str]:
    if not pending:
        return ["待收取列表是空的。"]
    lines = ["待收取 / 孵化中："]
    for egg in pending:
        ready = "可收取" if egg.st == 100 else "尚未就绪"
        st = "-" if egg.st is None else str(egg.st)
        lines.append(f"eggId={egg.egg_id} pmid={egg.pmid} st={st} {ready}")
    return lines


def _unhatched_line(egg_ids: list[int]) -> str:
    if not egg_ids:
        return "未孵化列表是空的。"
    preview = "、".join(str(egg_id) for egg_id in egg_ids[:12])
    suffix = "" if len(egg_ids) <= 12 else " …"
    return f"未孵化 {len(egg_ids)} 个：{preview}{suffix}"


def _phase_text(phase: str | None) -> str:
    return {
        "server": "服务器列表中的蛋，格子未知",
        "hatch_inflight": "孵化发送中",
        "hatching": "已提交，等待服务器就绪",
        "hatch_uncertain": "孵化结果不确定，格子继续占用",
        "readiness_timeout": "等待就绪超时，格子继续占用",
        "unmapped_timeout": "未能对应到格子，不再新开孵化",
        "ready": "服务器显示可收取",
        "collect_inflight": "收取发送中",
        "collect_uncertain": "收取结果不确定，不再自动收取",
    }.get(phase or "", phase or "未知阶段")
