"""Chinese incubator panel built only from a fresh or marked-stale snapshot."""
from __future__ import annotations

from dataclasses import dataclass

from src.game.eggs.names import NAME_NOTE, egg_display_name
from src.game.eggs.protocol import READY_ST, SLOT_IDS


PHASE_TEXT = {
    "idle": "空闲",
    "hatching": "孵化中",
    "ready": "可收取",
    "collecting": "正在收取",
    "draining": "正在停止并收取",
    "unknown": "未知",
}


@dataclass(frozen=True)
class ShownEgg:
    egg_id: int
    label: str
    slot_text: str
    readiness: str
    pmid: int | None
    st: int | None


@dataclass(frozen=True)
class IncubatorView:
    phase: str
    updated_at: str
    stale: bool
    stale_detail: str
    occupancy: int | None
    authoritative: bool
    ready_count: int | None
    submitted_hatches: int
    confirmed_collects: int
    eggs: tuple[ShownEgg, ...]
    name_note: str = NAME_NOTE


def confirmed_slot_text(record: dict | None) -> str:
    """Show a slot only when this client submitted that egg into it."""
    if not record or not record.get("hatch_sent"):
        return "未知格位"
    slot = record.get("slot")
    if slot in SLOT_IDS:
        return f"格 {slot}"
    return "未知格位"


def readiness_text(st: int | None) -> str:
    if st == READY_ST:
        return "可收取"
    if st is None:
        return "就绪未知"
    return "孵化中"


def make_shown_egg(
    egg_id: int,
    pmid: int | None,
    st: int | None,
    record: dict | None,
    *,
    species_id: int | None = None,
    catalog: dict[int, str] | None = None,
) -> ShownEgg:
    return ShownEgg(
        egg_id=egg_id,
        label=egg_display_name(egg_id, species_id, catalog),
        slot_text=confirmed_slot_text(record),
        readiness=readiness_text(st),
        pmid=pmid,
        st=st,
    )


def format_snapshot(view: IncubatorView) -> str:
    phase = PHASE_TEXT.get(view.phase, "未知")
    lines = [f"孵化格：{phase}"]
    if view.updated_at:
        lines.append(f"最近成功刷新：{view.updated_at}")
    else:
        lines.append("最近成功刷新：还没有")
    if view.authoritative and view.occupancy is not None:
        ready = view.ready_count if view.ready_count is not None else 0
        lines.append(f"占用：{view.occupancy}（来自本次 1-8），可收取 {ready}")
    elif view.stale and view.occupancy is not None:
        lines.append(f"上次成功时占用 {view.occupancy}。当前列表未知，不能当成 0。")
    else:
        lines.append("占用未知。没有把列表当成空的。")
    lines.append(
        f"本次已提交孵化 {view.submitted_hatches}，已确认收取 {view.confirmed_collects}"
    )
    lines.append("协议没有剩余时间，这里不显示倒计时。")
    if view.stale:
        lines.append(f"这次刷新失败：{view.stale_detail or '列表未知'}")
        lines.append("下面如果还有蛋，是上次成功的结果，不是当前空列表。")
    if view.authoritative and not view.eggs and not view.stale:
        lines.append("这次 1-8 是空的。")
    elif not view.eggs and not view.updated_at:
        lines.append("尚未成功刷新服务器列表。这不是空列表。")
    for egg in view.eggs:
        st = "-" if egg.st is None else str(egg.st)
        pmid = "-" if egg.pmid is None else str(egg.pmid)
        lines.append(
            f"{egg.label} {egg.slot_text} {egg.readiness} st={st} pmid={pmid}"
        )
    lines.append(view.name_note)
    return "\n".join(lines)
