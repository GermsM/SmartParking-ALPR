"""Indicateurs tableau de bord à partir des vraies données AccessLog / Vehicle."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func

import config
from models import AccessLog, db
from scope import vehicles_for_user


def _today_start_utc() -> datetime:
    return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def count_vehicles_present(role: str | None, site: str | None) -> int:
    """Dernière action par plaque : si c'est une entrée, le véhicule est considéré présent."""
    sq = db.session.query(
        AccessLog.plate_number.label("plate"),
        func.max(AccessLog.timestamp).label("max_ts"),
    )
    if site:
        sq = sq.filter(AccessLog.site == site)
    sq = sq.group_by(AccessLog.plate_number).subquery()

    q = (
        db.session.query(func.count())
        .select_from(sq)
        .join(
            AccessLog,
            and_(
                AccessLog.plate_number == sq.c.plate,
                AccessLog.timestamp == sq.c.max_ts,
                AccessLog.action == "entry",
            ),
        )
    )
    if site:
        q = q.filter(AccessLog.site == site)
    return int(q.scalar() or 0)


def count_access_today(role: str | None, site: str | None) -> int:
    start = _today_start_utc()
    q = AccessLog.query.filter(AccessLog.timestamp >= start, AccessLog.action == "entry")
    if site:
        q = q.filter(AccessLog.site == site)
    return q.count()


def count_alerts_today(role: str | None, site: str | None) -> int:
    start = _today_start_utc()
    q = AccessLog.query.filter(
        AccessLog.timestamp >= start,
        AccessLog.status.isnot(None),
        AccessLog.status.notin_(["authorized", "manual"]),
    )
    if site:
        q = q.filter(AccessLog.site == site)
    return q.count()


def count_forbidden_attempts_today(role: str | None, site: str | None) -> int:
    start = _today_start_utc()
    q = AccessLog.query.filter(AccessLog.timestamp >= start, AccessLog.status == "banned")
    if site:
        q = q.filter(AccessLog.site == site)
    return q.count()


def count_registered_active(role: str | None, site: str | None) -> int:
    return vehicles_for_user(role, site, status="active").count()


def count_registered_total(role: str | None, site: str | None) -> int:
    """Tous statuts — aligné sur la liste véhicules du gardien."""
    return vehicles_for_user(role, site).count()


def occupation_rate_percent(present: int, capacity: int) -> float:
    if capacity <= 0:
        return 0.0
    return min(100.0, round(100.0 * present / capacity, 1))


def get_dashboard_kpis(role: str | None, site: str | None, capacity: int) -> dict:
    present = count_vehicles_present(role, site)
    return {
        "vehicles_present": present,
        "access_today": count_access_today(role, site),
        "alerts_today": count_alerts_today(role, site),
        "forbidden_today": count_forbidden_attempts_today(role, site),
        "registered_active": count_registered_active(role, site),
        "registered_total": count_registered_total(role, site),
        "occupation_pct": occupation_rate_percent(present, capacity),
        "capacity": capacity,
        "site_label": site or "Tous les sites",
    }


def get_dashboard_kpis_by_site(role: str | None) -> list[dict]:
    if role == "admin":
        sites = list(config.SITE_CONFIG.keys())
    else:
        user_site = None
        if role and role != "admin":
            user_site = None
        sites = [user_site] if user_site else []
    if not sites:
        return []

    result = []
    for site_name in sites:
        capacity = config.get_site_capacity(site_name)
        kpi = get_dashboard_kpis(role, site_name, capacity)
        kpi["site"] = site_name
        result.append(kpi)
    return result
