"""Three incubator rows from this run's own hatch results plus the latest 1-8."""
from __future__ import annotations

from src.game.eggs.protocol import READY_ST, SLOT_IDS


def format_incubators(store, pending, *, fresh: bool) -> str:
    """1-8 has no slot id. Only a hatch this client submitted can fill 格 1/2/3."""
    records = store.snapshot()
    by_slot = {}
    owned = set()
    uncertain = False
    for record in records:
        phase = record.get("phase")
        if record.get("collect_blocked") or phase in {"hatch_uncertain", "collect_uncertain", "collect_inflight", "hatch_inflight"}:
            uncertain = True
        slot = record.get("slot")
        if record.get("hatch_sent") and slot in SLOT_IDS:
            by_slot[slot] = record
            owned.add(record["egg_id"])
    listed = {egg.egg_id: egg for egg in pending or []}
    lines = ["孵化器（只显示本机这次提交成功的格子）"]
    if not fresh:
        lines.append("列表未知，不能把三个格子当成空的。")
    for slot in SLOT_IDS:
        record = by_slot.get(slot)
        if record is None:
            lines.append(f"格 {slot}：空")
            continue
        egg = listed.get(record["egg_id"])
        status = egg.st if egg is not None else record.get("st")
        if record.get("phase") in {"hatch_uncertain", "collect_uncertain"} or record.get("collect_blocked"):
            state = "结果不确定"
        elif status == READY_ST:
            state = "可收取"
        elif record.get("phase") == "hatching":
            state = "孵化中"
        else:
            state = "占用"
        lines.append(f"格 {slot}：eggId={record['egg_id']} {state}")
    if fresh:
        unknown = [egg.egg_id for egg in pending or [] if egg.egg_id not in owned]
        if unknown:
            shown = "、".join(f"eggId={egg_id}" for egg_id in unknown[:8])
            lines.append(f"未知格位 {len(unknown)} 个：{shown}。没有对应到 1、2、3。")
        if not pending and uncertain:
            lines.append("服务器列表是空的，但还有未确认的发送，不能当成三个格子都已空。")
    return "\n".join(lines)
