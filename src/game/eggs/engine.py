"""Hatch and collect eggs without repeating an uncertain action.

The server status list does not include a slot id. A slot we have submitted
stays occupied until a correlated collect is confirmed, or the user clears the
local reservation. A status timeout is unknown, not an empty hatchery.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from src.game.eggs.protocol import (
    CMD_COLLECT,
    CMD_HATCH,
    CMD_STATUS,
    CMD_UNHATCHED,
    COLLECT_LOC,
    EXT_COLLECT,
    EXT_HATCH,
    READY_ST,
    SLOT_IDS,
    Limits,
    PendingEgg,
    QueryFailure,
    is_hatch_echo,
    is_invalid_egg_box,
    match_cmd,
    match_collect_ack,
    match_hatch_result,
    parse_status,
    parse_unhatched,
)
from src.game.eggs.panel import format_incubators
from src.game.eggs.store import HoldStore


Log = Callable[[str], None]


@dataclass
class QueryView:
    ok: bool
    kind: str
    detail: str
    pending: list[PendingEgg] | None
    unhatched: list[int] | None


@dataclass
class RunReport:
    ok: bool
    stopped: bool
    kind: str
    detail: str
    collected_ids: list[int] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)


class SystemClock:
    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float, should_stop: Callable[[], bool]) -> bool:
        end = self.now() + max(0.0, seconds)
        while self.now() < end:
            if should_stop():
                return False
            await asyncio.sleep(min(0.1, end - self.now()))
        return not should_stop()


class EggEngine:
    def __init__(
        self,
        transport,
        *,
        clock=None,
        store: HoldStore | None = None,
        limits: Limits | None = None,
        log: Log | None = None,
    ) -> None:
        self.transport = transport
        self.clock = clock or SystemClock()
        self.store = store or HoldStore()
        self.limits = limits or Limits()
        self.log = log or (lambda _message: None)
        self.stop_requested = False
        self.draining = False
        self.abort_requested = False
        self.watch: dict[int, float] = {}
        self._queue: list[int] = []
        self.collected_ids: list[int] = []
        self.absent_unconfirmed: list[int] = []
        self._fresh = False
        self._fresh_ids: set[int] = set()
        self._fresh_pending: list[PendingEgg] = []
        self.on_board = lambda _text: None
        if hasattr(transport, "allow_command"):
            transport.allow_command = self._allow_command
        self._in_run = False
        if hasattr(transport, "should_stop"):
            transport.should_stop = lambda: self.stop_requested or self.abort_requested

    def begin(self) -> None:
        """Drop a finished stop so the next explicit action is not cancelled."""
        self.draining = False
        self.abort_requested = False
        self.stop_requested = False

    def request_stop(self) -> None:
        if self.stop_requested:
            return
        self.draining = False
        self.stop_requested = True
        self.log("已按下停止。不再发送新的孵化或收取。已经写出的指令无法撤回。")

    def request_abort(self) -> None:
        if self.abort_requested:
            return
        self.abort_requested = True
        self.stop_requested = True
        self.log("已断开或关闭，停止收取。蛋留在游戏中。")

    def _allow_command(self, _ext_id: int, cmd: str) -> bool:
        if self.abort_requested or self.stop_requested:
            return False
        return True

    def board_text(self) -> str:
        return format_incubators(
            self.store, self._fresh_pending if self._fresh else None, fresh=self._fresh
        )

    def _publish_board(self) -> None:
        self.on_board(self.board_text())

    def _open(self) -> None:
        opener = getattr(self.transport, "open", None)
        if opener is not None:
            opener()

    async def query(self) -> QueryView:
        self._open()
        status = await self._fetch_status()
        if not status.ok:
            return status
        unhatched = await self._fetch_unhatched()
        if not unhatched.ok:
            return QueryView(
                False,
                unhatched.kind,
                unhatched.detail,
                status.pending,
                None,
            )
        self._sync(status.pending or [], unhatched.unhatched or [])
        return QueryView(
            True,
            "ok",
            f"待收取 {len(status.pending or [])} 个，未孵化 {len(unhatched.unhatched or [])} 个",
            status.pending,
            unhatched.unhatched,
        )

    async def run(self, *, confirmed: bool, retry_uncertain_collect: bool = False) -> RunReport:
        self._open()
        if not confirmed:
            self.log("没有确认，不发送孵化或收取。")
            return self._report(False, "confirmation", "没有确认，不发送孵化或收取")
        if not self.store.ok:
            return self._report(False, "store", self.store.error)
        self._in_run = True
        self.stop_requested = False
        self.draining = False
        try:
            return await self._run_body(retry_uncertain_collect=retry_uncertain_collect)
        finally:
            self._in_run = False
            self.stop_requested = False
            self.draining = False

    async def _run_body(self, *, retry_uncertain_collect: bool) -> RunReport:
        if self.abort_requested:
            return self._report(False, "stopped", "已断开，没有发送", stopped=True)
        if retry_uncertain_collect:
            self.log("已允许对仍在列表中的蛋再收取一次。已提交的孵化不会重发。")
            self.store.clear_collect_blocks()
        view = await self.query()
        if not view.ok:
            self.log(view.detail)
            return self._report(False, view.kind, view.detail, stopped=view.kind == "stopped")
        self.log(view.detail)
        while True:
            if self.stop_requested:
                return self._stopped_report()
            ready = self._ready_record()
            if ready is not None:
                outcome = await self._collect(ready)
                if outcome in {"collected", "skipped", "absent"}:
                    continue
                return self._finish_outcome(outcome)
            if self._unmapped():
                if self._waiting_unmapped():
                    outcome = await self._refresh_once()
                    if outcome != "ok":
                        if self.stop_requested or outcome == "stopped":
                            return self._stopped_report()
                        return self._finish_outcome(outcome)
                    if self.stop_requested:
                        return self._stopped_report()
                    if self._ready_record() is not None:
                        continue
                    if not await self._sleep_poll():
                        return self._stopped_report()
                    continue
                self._freeze_unmapped()
                self.log("服务器列表里仍有无法对应到孵化格的蛋，不再提交新的孵化。")
                return self._report(True, "held", "有无法对应格子的蛋，已停止新的孵化")
            if self._local_waiting():
                if self._waiting_local():
                    outcome = await self._refresh_once()
                    if outcome != "ok":
                        if self.stop_requested or outcome == "stopped":
                            return self._stopped_report()
                        return self._finish_outcome(outcome)
                    if self.stop_requested:
                        return self._stopped_report()
                    if self._ready_record() is not None:
                        continue
                    if not await self._sleep_poll():
                        return self._stopped_report()
                    continue
                self._freeze_local()
                continue
            egg_id = self._next_egg()
            slot = self._free_slot()
            if egg_id is None or slot is None:
                detail = "没有可以安全提交的新蛋" if egg_id is None else "三个孵化格都已占用或结果不确定"
                self.log(detail)
                return self._report(True, "idle", detail)
            outcome = await self._hatch(egg_id, slot)
            if self.stop_requested or self.abort_requested:
                return self._stopped_report()
            if outcome == "accepted":
                self._ensure_watch(egg_id)
                continue
            if outcome == "uncertain":
                continue
            return self._finish_outcome(outcome)

    async def _fetch_status(self) -> QueryView:
        result = await self.transport.exchange(
            EXT_HATCH, CMD_STATUS, {}, match_cmd(CMD_STATUS), self.limits.query_timeout
        )
        view = self._parse_exchange(result, parse_status, "1-8", pending=True)
        if view.ok:
            self._note_fresh(view.pending or [])
        else:
            self._fresh = False
            self._publish_board()
        return view

    async def _fetch_unhatched(self) -> QueryView:
        result = await self.transport.exchange(
            EXT_HATCH,
            CMD_UNHATCHED,
            {},
            match_cmd(CMD_UNHATCHED),
            self.limits.query_timeout,
        )
        return self._parse_exchange(result, parse_unhatched, "1=1", pending=False)

    def _parse_exchange(self, result, parser, label: str, *, pending: bool) -> QueryView:
        if result.kind != "message":
            detail = {
                "timeout": f"{label} 查询超时，列表未知，不能当成空列表",
                "stopped": f"已停止，{label} 列表未知",
                "disconnected": f"连接已断开，{label} 列表未知",
                "blocked": result.detail or "服务器要求停止自动发送",
            }.get(result.kind, result.detail or f"{label} 查询失败")
            self.log(detail)
            return QueryView(False, result.kind, detail, None, None)
        try:
            parsed = parser(result.message or {})
        except QueryFailure as exc:
            self.log(str(exc))
            return QueryView(False, "malformed", str(exc), None, None)
        if pending:
            return QueryView(True, "ok", "", parsed, None)
        return QueryView(True, "ok", "", None, parsed)

    def _sync(self, pending: list[PendingEgg], unhatched: list[int]) -> None:
        for egg in pending:
            current = self.store.get(egg.egg_id) or {
                "egg_id": egg.egg_id,
                "slot": None,
                "pmid": egg.pmid,
                "st": egg.st,
                "phase": "server",
                "hatch_sent": False,
                "collect_blocked": False,
            }
            current["pmid"] = egg.pmid
            current["st"] = egg.st
            if current.get("phase") in {None, "server", "ready", "hatching"} and egg.st == READY_ST:
                if not current.get("collect_blocked"):
                    current["phase"] = "ready"
            self.store.upsert(current)
        self._queue = [
            egg_id for egg_id in unhatched if not self.store.hatch_sent(egg_id)
        ]

    def _apply_status(self, pending: list[PendingEgg]) -> None:
        by_id = {egg.egg_id: egg for egg in pending}
        for egg_id, record in list(self.store.records.items()):
            egg = by_id.get(egg_id)
            if egg is None:
                continue
            record["pmid"] = egg.pmid
            record["st"] = egg.st
            if egg.st == READY_ST and not record.get("collect_blocked"):
                if record.get("phase") in {"server", "hatching", "ready", "hatch_inflight"}:
                    record["phase"] = "ready"
            self.store.upsert(record)

    def _note_fresh(self, pending: list[PendingEgg]) -> None:
        """A successful 1-8 is the incubator. Absent history is not a current egg."""
        self._fresh = True
        self._fresh_pending = list(pending)
        self._fresh_ids = {egg.egg_id for egg in pending}
        for egg_id, record in list(self.store.records.items()):
            if egg_id in self._fresh_ids:
                if record.get("phase") == "audit_absent":
                    record["phase"] = "server"
                    self.store.upsert(record)
                continue
            phase = record.get("phase")
            if phase in {"hatch_inflight", "collect_inflight"}:
                continue
            if (
                record.get("hatch_sent")
                and record.get("slot") in SLOT_IDS
                and phase in {"hatching", "readiness_timeout", "hatch_uncertain"}
            ):
                if record.get("st") == READY_ST:
                    record["st"] = None
                    self.store.upsert(record)
                continue
            if phase in {"collect_uncertain", "collect_inflight"} or record.get("collect_blocked"):
                record["phase"] = "audit_absent"
                record["st"] = None
                record["slot"] = None
                record["collect_blocked"] = True
                self.store.upsert(record)
                continue
            self.store.remove(egg_id)
        self._publish_board()

    def _uncertain_pending(self) -> bool:
        return any(
            record.get("collect_blocked")
            or record.get("phase") in {
                "hatch_inflight",
                "collect_inflight",
                "hatch_uncertain",
                "collect_uncertain",
            }
            for record in self.store.records.values()
        )

    def _ready_record(self) -> dict | None:
        if not self._fresh:
            return None
        ready = [
            record
            for record in self.store.records.values()
            if record.get("egg_id") in self._fresh_ids
            and record.get("st") == READY_ST
            and record.get("pmid")
            and not record.get("collect_blocked")
        ]
        ready.sort(key=lambda record: (record.get("slot") or 99, record["egg_id"]))
        return dict(ready[0]) if ready else None

    def _unmapped(self) -> list[dict]:
        rows = []
        for record in self.store.records.values():
            if record.get("slot") is not None:
                continue
            if record.get("phase") == "audit_absent":
                continue
            if self._fresh and record.get("egg_id") not in self._fresh_ids:
                continue
            rows.append(record)
        return rows

    def _waiting_unmapped(self) -> bool:
        waiting = [
            record
            for record in self._unmapped()
            if record.get("st") != READY_ST and not record.get("collect_blocked")
            and record.get("phase") != "unmapped_timeout"
        ]
        return any(not self._overdue(record["egg_id"]) for record in waiting)

    def _freeze_unmapped(self) -> None:
        for record in self._unmapped():
            if record.get("phase") == "server" and record.get("st") != READY_ST:
                record["phase"] = "unmapped_timeout"
                self.store.upsert(record)

    def _local_waiting(self) -> list[dict]:
        return [
            record
            for record in self.store.records.values()
            if record.get("slot") in SLOT_IDS
            and record.get("phase") == "hatching"
            and record.get("st") != READY_ST
        ]

    def _waiting_local(self) -> bool:
        return any(not self._overdue(record["egg_id"]) for record in self._local_waiting())

    def _freeze_local(self) -> None:
        for record in self._local_waiting():
            if self._overdue(record["egg_id"]):
                record["phase"] = "readiness_timeout"
                self.store.upsert(record)
                self.log(
                    f"eggId={record['egg_id']} 在格 {record['slot']} 仍未就绪。"
                    "该格继续视为占用，不会放入下一个蛋。"
                )

    def _ensure_watch(self, egg_id: int) -> None:
        self.watch.setdefault(egg_id, self.clock.now())

    def _overdue(self, egg_id: int) -> bool:
        self._ensure_watch(egg_id)
        return self.clock.now() - self.watch[egg_id] >= self.limits.readiness_limit

    def _next_egg(self) -> int | None:
        while self._queue:
            egg_id = self._queue.pop(0)
            if not self.store.hatch_sent(egg_id):
                return egg_id
        return None

    def _free_slot(self) -> int | None:
        used = {
            record.get("slot")
            for record in self.store.records.values()
            if record.get("slot") in SLOT_IDS
        }
        for slot in SLOT_IDS:
            if slot not in used:
                return slot
        return None

    async def _refresh_once(self) -> str:
        if self.abort_requested:
            return "stopped"
        status = await self._fetch_status()
        if not status.ok:
            return status.kind
        self._apply_status(status.pending or [])
        return "ok"

    async def _sleep_poll(self) -> bool:
        return await self.clock.sleep(self.limits.poll_interval, lambda: self.stop_requested)

    async def _hatch(self, egg_id: int, slot: int) -> str:
        if self.stop_requested or self.abort_requested:
            self._queue.insert(0, egg_id)
            return "stopped"
        self.store.upsert(
            {
                "egg_id": egg_id,
                "slot": slot,
                "pmid": None,
                "st": None,
                "phase": "hatch_inflight",
                "hatch_sent": True,
                "collect_blocked": False,
            }
        )
        self.log(f"提交孵化 eggId={egg_id}，格 {slot}")
        result = await self.transport.exchange(
            EXT_HATCH,
            CMD_HATCH,
            {"eggId": egg_id, "id": slot},
            match_hatch_result(egg_id, slot),
            self.limits.echo_timeout,
        )
        if not result.sent:
            self.store.undo_unsent(egg_id, "hatch_inflight")
            self._queue.insert(0, egg_id)
            self.log("孵化指令没有发出。")
            return result.kind
        if result.kind != "message":
            self._mark(egg_id, slot, "hatch_uncertain", hatch_sent=True)
            self.log(
                f"eggId={egg_id} 的孵化回显不确定（{result.kind}）。"
                f"格 {slot} 保持占用，不再提交这个蛋。"
            )
            if result.kind in {"stopped", "disconnected", "blocked"}:
                return result.kind
            return "uncertain"
        message = result.message or {}
        if not is_hatch_echo(message, egg_id, slot):
            self._mark(egg_id, slot, "hatch_uncertain", hatch_sent=True)
            self.log(
                f"eggId={egg_id} 收到无效蛋提示，但提示没有 eggId。"
                f"格 {slot} 保持占用，不再提交。"
            )
            return "uncertain"
        notice = await self.transport.wait(
            is_invalid_egg_box, self.limits.message_box_timeout
        )
        if notice.kind == "message":
            self._mark(egg_id, slot, "hatch_uncertain", hatch_sent=True)
            self.log(
                f"eggId={egg_id} 已有匹配回显，同时又收到无效蛋提示。"
                "协议没有把该提示关联到具体蛋，格子保持占用，不再发送。"
            )
            return "uncertain"
        self._mark(egg_id, slot, "hatching", hatch_sent=True)
        self.log(f"eggId={egg_id} 已提交到格 {slot}。接下来只查询服务器是否就绪。")
        self._publish_board()
        if notice.kind in {"stopped", "disconnected", "blocked"}:
            return notice.kind
        return "accepted"

    async def _collect(self, record: dict) -> str:
        if self.stop_requested or self.abort_requested:
            return "stopped"
        egg_id = int(record["egg_id"])
        pmid = int(record["pmid"])
        if not self._fresh or egg_id not in self._fresh_ids:
            self.log(f"eggId={egg_id} 不在最新的 1-8 里，不收取。")
            current = self.store.get(egg_id)
            if current and current.get("phase") == "ready":
                current["st"] = None
                current["phase"] = "server"
                self.store.upsert(current)
            return "skipped"
        slot = record.get("slot")
        self.store.upsert(
            {
                **record,
                "phase": "collect_inflight",
                "collect_blocked": True,
                "pmid": pmid,
            }
        )
        self.log(f"提交收取 eggId={egg_id} pmid={pmid}")
        result = await self.transport.exchange(
            EXT_COLLECT,
            CMD_COLLECT,
            {"loc": COLLECT_LOC, "id": pmid},
            match_collect_ack(pmid),
            self.limits.collect_timeout,
        )
        if not result.sent:
            self.store.undo_unsent(egg_id, "collect_inflight")
            self.log("收取指令没有发出。")
            return result.kind
        if result.kind != "message":
            if result.kind == "timeout":
                return await self._reconcile_collect_timeout(record, pmid, slot)
            self._mark_collect_uncertain(record, pmid, slot)
            self.log(
                f"eggId={egg_id} 的收取回执不确定（{result.kind}）。不再发送 1223。"
            )
            return "uncertain"
        status = await self._fetch_status()
        if not status.ok:
            self._mark_collect_uncertain(record, pmid, slot)
            self.log(f"eggId={egg_id} 的收取后列表{status.detail}。不再发送 1223。")
            return "uncertain"
        self._apply_status(status.pending or [])
        if any(egg.egg_id == egg_id for egg in status.pending or []):
            latest = self.store.get(egg_id) or record
            self._mark_collect_uncertain(latest, pmid, slot)
            self.log(f"eggId={egg_id} 的回执已匹配，但列表里仍然有它。不再发送 1223。")
            return "uncertain"
        self.store.remove(egg_id)
        self.watch.pop(egg_id, None)
        self.collected_ids.append(egg_id)
        self.log(f"收取已确认：eggId={egg_id} 回执已对齐，并已从随后的 1-8 消失。")
        return "collected"

    async def _reconcile_collect_timeout(self, record: dict, pmid: int, slot: int | None) -> str:
        """One read-only 1-8 after a missed 2223. Absence is not a confirmed collect."""
        egg_id = int(record["egg_id"])
        status = await self._fetch_status()
        if not status.ok or status.pending is None:
            self._mark_collect_uncertain(record, pmid, slot)
            self.log(f"eggId={egg_id} 收取回执超时，随后的 1-8 未知。格子继续占用。不再发送 1223。")
            return "uncertain"
        if any(egg.egg_id == egg_id for egg in status.pending):
            latest = self.store.get(egg_id) or record
            self._mark_collect_uncertain(latest, pmid, slot)
            self.log(f"eggId={egg_id} 收取回执超时，蛋仍在 1-8。不再发送 1223。")
            return "uncertain"
        if self._other_uncertain(egg_id):
            self._audit_collect(record, pmid, slot, free_slot=False)
            self.log(
                f"eggId={egg_id} 收取回执超时，列表里没有它。"
                "还有其他未确认发送，先暂停。这不是已确认收取。"
            )
            return "uncertain"
        self._audit_collect(record, pmid, slot, free_slot=True)
        self.absent_unconfirmed.append(egg_id)
        self.log(
            f"eggId={egg_id} 收取回执超时。随后的 1-8 里没有它，格子已释放。"
            "这不是已确认收取，不会重发 1223。"
        )
        return "absent"

    def _other_uncertain(self, egg_id: int) -> bool:
        for other_id, record in self.store.records.items():
            if other_id == egg_id:
                continue
            if record.get("phase") in {
                "hatch_inflight",
                "collect_inflight",
                "hatch_uncertain",
                "collect_uncertain",
            }:
                return True
        return False

    def _audit_collect(self, record: dict, pmid: int, slot: int | None, *, free_slot: bool) -> None:
        self.store.upsert(
            {
                "egg_id": record["egg_id"],
                "slot": None if free_slot else (slot if slot in SLOT_IDS else record.get("slot")),
                "pmid": pmid,
                "st": None,
                "phase": "audit_absent",
                "hatch_sent": bool(record.get("hatch_sent")),
                "collect_blocked": True,
            }
        )
        if free_slot:
            self.watch.pop(int(record["egg_id"]), None)
        self._publish_board()

    def _mark(self, egg_id: int, slot: int, phase: str, *, hatch_sent: bool) -> None:
        current = self.store.get(egg_id) or {}
        self.store.upsert(
            {
                "egg_id": egg_id,
                "slot": slot,
                "pmid": current.get("pmid"),
                "st": current.get("st"),
                "phase": phase,
                "hatch_sent": hatch_sent,
                "collect_blocked": bool(current.get("collect_blocked")),
            }
        )

    def _mark_collect_uncertain(self, record: dict, pmid: int, slot: int | None) -> None:
        self.store.upsert(
            {
                "egg_id": record["egg_id"],
                "slot": slot if slot in SLOT_IDS else record.get("slot"),
                "pmid": pmid,
                "st": record.get("st"),
                "phase": "collect_uncertain",
                "hatch_sent": bool(record.get("hatch_sent")),
                "collect_blocked": True,
            }
        )

    def _stopped_report(self) -> RunReport:
        detail = "已停止。不再发送新的孵化或收取。已经写出的指令无法撤回。"
        self.log(detail)
        return self._report(True, "stopped", detail, stopped=True)

    async def collect_ready(self, *, confirmed: bool, retry_uncertain_collect: bool = False) -> RunReport:
        """Collect only eggs that a fresh 1-8 lists as ready. Do not hatch or wait."""
        self._open()
        if not confirmed:
            self.log("没有确认，不发送收取。")
            return self._report(False, "confirmation", "没有确认，不发送收取")
        if self._in_run:
            return self._report(False, "busy", "已有操作在进行，没有发送收取")
        self._in_run = True
        self.stop_requested = False
        collected: list[int] = []
        try:
            if retry_uncertain_collect:
                self.log("已允许对仍在最新列表中的蛋再收取一次。")
                self.store.clear_collect_blocks()
            status = await self._fetch_status()
            if not status.ok or status.pending is None:
                self.log(status.detail)
                return self._report(False, status.kind, status.detail)
            if self.stop_requested:
                return self._stopped_report()
            ready = [
                egg for egg in status.pending
                if egg.st == READY_ST and egg.pmid > 0 and not self._blocked(egg.egg_id, egg.pmid)
            ]
            ready.sort(key=lambda egg: egg.egg_id)
            if not ready:
                detail = "最新 1-8 里没有可收取的蛋。没有孵化，也没有等待未就绪的蛋。"
                self.log(detail)
                return self._report(True, "idle", detail)
            for egg in ready:
                if self.stop_requested:
                    return self._stopped_report()
                outcome = await self._collect(self._server_record(egg))
                if outcome == "collected":
                    collected.append(egg.egg_id)
                    continue
                return self._finish_outcome(outcome)
            detail = f"已收取 {len(collected)} 个当前就绪的蛋。没有孵化，也没有等待未就绪的蛋。"
            self.log(detail)
            return self._report(True, "collected", detail)
        finally:
            self._in_run = False
            self.stop_requested = False

    def _blocked(self, egg_id: int, pmid: int) -> bool:
        record = self.store.get(egg_id)
        if record and (record.get("collect_blocked") or record.get("phase") == "collect_uncertain"):
            return True
        return self.store.collect_blocked(pmid)

    def _server_record(self, egg: PendingEgg) -> dict:
        current = self.store.get(egg.egg_id) or {}
        slot = current.get("slot") if current.get("slot") in SLOT_IDS else None
        return {
            "egg_id": egg.egg_id,
            "slot": slot,
            "pmid": egg.pmid,
            "st": egg.st,
            "phase": "ready",
            "hatch_sent": bool(current.get("hatch_sent")),
            "collect_blocked": False,
        }

    def _finish_outcome(self, outcome: str) -> RunReport:
        detail = {
            "stopped": "已停止。已提交的蛋仍留在游戏中。",
            "disconnected": "连接已断开。已提交的蛋仍留在游戏中，没有继续发送。",
            "timeout": "查询超时。列表未知，没有把它当成空列表，也没有继续发送。",
            "malformed": "回包格式无法识别，已停止发送。",
            "blocked": "服务器要求停止自动发送。",
            "uncertain": "结果不确定，已停止继续发送。",
        }.get(outcome, "已停止发送。")
        self.log(detail)
        return self._report(
            outcome in {"stopped", "uncertain"},
            outcome,
            detail,
            stopped=outcome == "stopped",
        )

    def _report(
        self,
        ok: bool,
        kind: str,
        detail: str,
        *,
        stopped: bool = False,
    ) -> RunReport:
        return RunReport(
            ok=ok,
            stopped=stopped or self.stop_requested,
            kind=kind,
            detail=detail,
            collected_ids=list(self.collected_ids),
            held=self.store.snapshot(),
        )
