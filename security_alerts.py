"""Cache plaques + signalement UI (banni / inconnu) et logs."""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_banned_cache: frozenset[str] = frozenset()
_active_cache: dict[str, dict] = {}
_cache_time: float = 0.0
CACHE_TTL_SEC = 18.0

_alert_seq = 0
_alert_plate = ""
_alert_type = ""  # banned | unknown | forbidden_type
_alert_owner_phone = ""
_alert_owner_email = ""
_alert_ts: float = 0.0
_last_plate_signal: dict[str, float] = {}
_SIGNAL_INTERVAL_SEC = 16.0
_last_log_banned: dict[str, float] = {}
_LOG_INTERVAL_SEC = 55.0


def invalidate_banned_vehicle_cache() -> None:
    global _cache_time
    with _lock:
        _cache_time = 0.0


def _refresh_vehicle_cache(app) -> None:
    global _banned_cache, _active_cache, _cache_time
    with app.app_context():
        from models import Vehicle

        rows = Vehicle.query.all()
        banned = set()
        active = {}
        for v in rows:
            p = (v.plate_number or "").upper().strip()
            if not p:
                continue
            if v.status == "banned":
                banned.add(p)
            active[p] = {
                "status": v.status,
                "owner_phone": v.owner_phone or "",
                "owner_email": v.owner_email or "",
                "owner_name": v.owner_name or "",
            }
    with _lock:
        _banned_cache = frozenset(banned)
        _active_cache = active
        _cache_time = time.time()


def get_banned_plates(app) -> frozenset[str]:
    now = time.time()
    with _lock:
        if _cache_time and (now - _cache_time) < CACHE_TTL_SEC:
            return _banned_cache
    _refresh_vehicle_cache(app)
    with _lock:
        return _banned_cache


def get_vehicle_info(app, plate: str) -> dict | None:
    plate = (plate or "").upper().strip()
    now = time.time()
    with _lock:
        stale = not _cache_time or (now - _cache_time) >= CACHE_TTL_SEC
    if stale:
        _refresh_vehicle_cache(app)
    with _lock:
        return _active_cache.get(plate)


def _signal_alert(plate: str, alert_type: str, owner_phone: str = "", owner_email: str = "") -> None:
    global _alert_seq, _alert_plate, _alert_type, _alert_owner_phone, _alert_owner_email, _alert_ts
    plate = (plate or "").upper().strip()
    if not plate:
        return
    now = time.time()
    key = f"{alert_type}:{plate}"
    with _lock:
        last = _last_plate_signal.get(key, 0.0)
        if now - last < _SIGNAL_INTERVAL_SEC:
            return
        _last_plate_signal[key] = now
        _alert_seq += 1
        _alert_plate = plate
        _alert_type = alert_type
        _alert_owner_phone = owner_phone
        _alert_owner_email = owner_email
        _alert_ts = now


def signal_banned_plate_detected(plate: str, owner_phone: str = "", owner_email: str = "") -> None:
    _signal_alert(plate, "banned", owner_phone, owner_email)


def signal_unknown_plate_detected(plate: str) -> None:
    _signal_alert(plate, "unknown")


def signal_forbidden_type_detected(label: str) -> None:
    _signal_alert(label, "forbidden_type")


def get_security_alert_state() -> dict:
    with _lock:
        return {
            "id": _alert_seq,
            "plate": _alert_plate,
            "type": _alert_type,
            "owner_phone": _alert_owner_phone,
            "owner_email": _alert_owner_email,
            "ts": _alert_ts,
        }


def log_banned_detection_throttled(app, plate: str, site: str | None, guardian_id: int | None) -> None:
    plate = (plate or "").upper().strip()
    if not plate:
        return
    now = time.time()
    with _lock:
        last = _last_log_banned.get(plate, 0.0)
        if now - last < _LOG_INTERVAL_SEC:
            return
        _last_log_banned[plate] = now
    with app.app_context():
        from models import AccessLog, Vehicle, db

        v = Vehicle.query.filter_by(plate_number=plate).first()
        db.session.add(
            AccessLog(
                plate_number=plate,
                vehicle_id=v.id if v else None,
                action="entry",
                status="banned",
                site=site or (v.site_authorized if v else None),
                guardian_id=guardian_id,
            )
        )
        db.session.commit()
