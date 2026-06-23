"""Construction des lignes « visite » (entrée + sortie) pour export CSV."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from models import AccessLog


@dataclass
class VisitRow:
    plate: str
    site: str | None
    entry_at: datetime | None
    exit_at: datetime | None
    duration_minutes: int | None
    entry_status: str | None
    exit_status: str | None
    guardian_name: str | None = None
    remark: str | None = None


def _minutes_between(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def _guardian_label(log: AccessLog | None, guardian_map: dict[int, str]) -> str | None:
    if not log or not log.guardian_id:
        return None
    return guardian_map.get(log.guardian_id)


def pair_access_logs(logs: Iterable[AccessLog], guardian_map: dict[int, str] | None = None) -> list[VisitRow]:
    """Associe chaque sortie à la plus ancienne entrée ouverte (FIFO) par plaque."""
    guardian_map = guardian_map or {}
    pending: dict[str, deque[AccessLog]] = defaultdict(deque)
    rows: list[VisitRow] = []
    orphan_exits: list[VisitRow] = []

    for log in logs:
        plate = (log.plate_number or "").strip().upper()
        act = (log.action or "").strip().lower()
        if not plate:
            continue
        if act == "entry":
            pending[plate].append(log)
        elif act == "exit":
            dq = pending.get(plate)
            if dq:
                ent = dq.popleft()
                dur = log.duration_minutes
                if dur is None:
                    dur = _minutes_between(ent.timestamp, log.timestamp)
                rows.append(
                    VisitRow(
                        plate=plate,
                        site=ent.site or log.site,
                        entry_at=ent.timestamp,
                        exit_at=log.timestamp,
                        duration_minutes=dur,
                        entry_status=ent.status,
                        exit_status=log.status,
                        guardian_name=_guardian_label(ent, guardian_map),
                        remark=None,
                    )
                )
            else:
                orphan_exits.append(
                    VisitRow(
                        plate=plate,
                        site=log.site,
                        entry_at=None,
                        exit_at=log.timestamp,
                        duration_minutes=None,
                        entry_status=None,
                        exit_status=log.status,
                        remark="Sortie sans entrée appariée",
                    )
                )

    open_visits: list[VisitRow] = []
    for plate, dq in pending.items():
        for ent in dq:
            open_visits.append(
                VisitRow(
                    plate=plate,
                    site=ent.site,
                    entry_at=ent.timestamp,
                    exit_at=None,
                    duration_minutes=None,
                    entry_status=ent.status,
                    exit_status=None,
                    guardian_name=_guardian_label(ent, guardian_map),
                    remark="Visite en cours (pas de sortie enregistrée)",
                )
            )

    rows.extend(orphan_exits)
    rows.extend(open_visits)

    def sort_key(r: VisitRow) -> datetime:
        return r.entry_at or r.exit_at or datetime.min

    rows.sort(key=sort_key)
    return rows
