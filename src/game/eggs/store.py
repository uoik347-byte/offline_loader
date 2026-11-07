"""Local slot and uncertain-action memory. No account secrets are stored."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


HOLD_PHASES = frozenset(
    {
        "server",
        "hatch_inflight",
        "hatching",
        "hatch_uncertain",
        "readiness_timeout",
        "unmapped_timeout",
        "ready",
        "collect_inflight",
        "collect_uncertain",
        "audit_absent",
    }
)
class StoreError(ValueError):
    pass


class HoldStore:
    def __init__(self) -> None:
        self.ok = True
        self.error = ""
        self.records: dict[int, dict] = {}

    def snapshot(self) -> list[dict]:
        return [dict(item) for item in self.records.values()]

    def get(self, egg_id: int) -> dict | None:
        item = self.records.get(egg_id)
        return dict(item) if item is not None else None

    def upsert(self, record: dict) -> None:
        cleaned = _clean_record(record)
        self.records[cleaned["egg_id"]] = cleaned
        self._reject_duplicate_slots()
        self.save()

    def remove(self, egg_id: int) -> None:
        self.records.pop(egg_id, None)
        self.save()

    def hatch_sent(self, egg_id: int) -> bool:
        item = self.records.get(egg_id)
        return bool(item and item.get("hatch_sent"))

    def collect_blocked(self, pmid: int) -> bool:
        return any(
            item.get("pmid") == pmid and item.get("collect_blocked")
            for item in self.records.values()
        )

    def clear_collect_blocks(self) -> None:
        for item in self.records.values():
            if item.get("collect_blocked"):
                item["collect_blocked"] = False
                if item.get("phase") in {"collect_inflight", "collect_uncertain", "audit_absent"}:
                    item["phase"] = "ready"
        self.save()

    def clear_slot_holds(self) -> None:
        """Forget local slot reservations. Does not send anything."""
        kept: dict[int, dict] = {}
        for egg_id, item in self.records.items():
            if item.get("collect_blocked"):
                updated = dict(item)
                updated["slot"] = None
                kept[egg_id] = updated
        self.records = kept
        self.save()

    def undo_unsent(self, egg_id: int, phase: str) -> None:
        item = self.records.get(egg_id)
        if item is None or item.get("phase") != phase:
            return
        if phase == "hatch_inflight":
            self.records.pop(egg_id, None)
        elif phase == "collect_inflight":
            item["collect_blocked"] = False
            item["phase"] = "ready"
        self.save()

    def save(self) -> None:
        return

    def _reject_duplicate_slots(self) -> None:
        seen: dict[int, int] = {}
        for item in self.records.values():
            slot = item.get("slot")
            if slot is None:
                continue
            previous = seen.get(slot)
            if previous is not None and previous != item["egg_id"]:
                raise StoreError(f"孵化格 {slot} 被重复占用")
            seen[slot] = item["egg_id"]


class JsonHoldStore(HoldStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.records = {}
            self.ok = True
            self.error = ""
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            records = _parse_file(raw)
        except (OSError, json.JSONDecodeError, StoreError):
            self.records = {}
            self.ok = False
            self.error = "本地孵化记录无法读取，已禁止开始，避免在不确定状态下再次发送"
            return
        self.records = records
        self.ok = True
        self.error = ""

    def bind_scope(self, account: str, char_id: int) -> None:
        """Separate one character's notes from another's. The path is only a hash."""
        digest = hashlib.sha256(
            f"{account.strip()}\n{int(char_id)}".encode("utf-8")
        ).hexdigest()[:16]
        scoped = self.path.parent / f"egg-hatch-state-{digest}.json"
        if scoped == self.path:
            return
        self.path = scoped
        self.load()

    def save(self) -> None:
        if not self.ok:
            return
        payload = {
            "version": 1,
            "records": [dict(item) for item in self.records.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _parse_file(raw: object) -> dict[int, dict]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise StoreError("孵化记录版本无法识别")
    rows = raw.get("records")
    if not isinstance(rows, list):
        raise StoreError("孵化记录格式无效")
    records: dict[int, dict] = {}
    seen_slots: dict[int, int] = {}
    for row in rows:
        cleaned = _clean_record(row)
        egg_id = cleaned["egg_id"]
        if egg_id in records:
            raise StoreError("孵化记录有重复 eggId")
        slot = cleaned.get("slot")
        if slot is not None:
            if slot in seen_slots:
                raise StoreError("孵化记录有重复孵化格")
            seen_slots[slot] = egg_id
        records[egg_id] = cleaned
    return records


def _clean_record(record: object) -> dict:
    if not isinstance(record, dict):
        raise StoreError("孵化记录项无效")
    egg_id = record.get("egg_id")
    slot = record.get("slot")
    pmid = record.get("pmid")
    status = record.get("st")
    phase = record.get("phase")
    if not isinstance(egg_id, int) or isinstance(egg_id, bool) or egg_id <= 0:
        raise StoreError("孵化记录 eggId 无效")
    if slot is not None and (
        isinstance(slot, bool) or not isinstance(slot, int) or slot not in (1, 2, 3)
    ):
        raise StoreError("孵化记录的格子无效")
    if pmid is not None and (
        isinstance(pmid, bool) or not isinstance(pmid, int) or pmid <= 0
    ):
        raise StoreError("孵化记录的收取 id 无效")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
        raise StoreError("孵化记录的状态无效")
    if phase not in HOLD_PHASES:
        raise StoreError("孵化记录的阶段无效")
    if not isinstance(record.get("hatch_sent"), bool):
        raise StoreError("孵化记录的提交标记无效")
    if not isinstance(record.get("collect_blocked"), bool):
        raise StoreError("孵化记录的收取标记无效")
    return {
        "egg_id": egg_id,
        "slot": slot,
        "pmid": pmid,
        "st": status,
        "phase": phase,
        "hatch_sent": record["hatch_sent"],
        "collect_blocked": record["collect_blocked"],
    }
