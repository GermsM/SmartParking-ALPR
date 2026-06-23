import threading
import time
from datetime import datetime

_lock = threading.Lock()
_present: dict[str, dict] = {}  # plate -> {last_seen, site, vehicle_id, status, entry_log_id}
_last_event: dict[str, float] = {}
_EVENT_COOLDOWN_SEC = 2.0  # Cooldown reduit car la double-lecture filtre dejà les doublons

def _now_ts() -> float:
    return time.time()


def lookup_vehicle_status(app, plate: str) -> tuple[str, int | None, str | None, object | None]:
    """Retourne (status, vehicle_id, site_authorized, vehicle_obj)."""
    with app.app_context():
        from models import Vehicle

        v = Vehicle.query.filter_by(plate_number=plate).first()
        if not v:
            return "unknown", None, None, None
        if v.status == "banned":
            return "banned", v.id, v.site_authorized, v
        if v.status == "pending":
            return "pending", v.id, v.site_authorized, v
        return "authorized", v.id, v.site_authorized, v


def confirm_entry_in_db(app, plate: str, site: str | None, guardian_id: int | None) -> None:
    """Enregistre officiellement l'entree du vehicule apres confirmation par la double-lecture."""
    plate = (plate or "").upper().strip()
    if not plate or not site:
        return

    status, vehicle_id, site_auth, vehicle = lookup_vehicle_status(app, plate)
    now = _now_ts()

    with app.app_context():
        from models import AccessLog, db, Site
        
        # Resoudre site_id
        s_obj = Site.query.filter_by(name=site).first()
        s_id = s_obj.id if s_obj else None

        log = AccessLog(
            plate_number=plate,
            vehicle_id=vehicle_id,
            action="entry",
            status=status,
            site=site,
            site_id=s_id,
            guardian_id=guardian_id,
        )
        db.session.add(log)
        db.session.commit()
        entry_id = log.id
        entry_at = log.timestamp

    with _lock:
        _present[plate] = {
            "last_seen": now,
            "site": site,
            "vehicle_id": vehicle_id,
            "status": status,
            "entry_log_id": entry_id,
            "entry_at": entry_at,
        }
        _last_event[plate] = now
    print(f"[ACCES] Entree confirmee pour la plaque {plate} sur le site {site}")


def confirm_exit_in_db(app, plate: str, site: str | None, guardian_id: int | None) -> None:
    """Enregistre officiellement la sortie du vehicule apres confirmation par la double-lecture."""
    plate = (plate or "").upper().strip()
    if not plate or not site:
        return

    now = _now_ts()
    with _lock:
        info = _present.pop(plate, None)
        _last_event[plate] = now

    if info:
        with app.app_context():
            from models import AccessLog, db, Site
            
            # Resoudre site_id
            s_obj = Site.query.filter_by(name=site).first()
            s_id = s_obj.id if s_obj else None

            entry_at = info.get("entry_at") or datetime.utcnow()
            dur = int((datetime.utcnow() - entry_at).total_seconds() // 60)
            
            log = AccessLog(
                plate_number=plate,
                vehicle_id=info.get("vehicle_id"),
                action="exit",
                status=info.get("status", "authorized"),
                site=site,
                site_id=s_id,
                guardian_id=guardian_id,
                duration_minutes=dur,
            )
            db.session.add(log)
            db.session.commit()
        print(f"[ACCES] Sortie confirmee pour la plaque {plate} du site {site} apres {dur} minutes")


def process_plate_detection(app, plate: str, site: str | None, guardian_id: int | None) -> str | None:
    """
    Rétrocompatibilite.
    Dans le cadre de la double-lecture, ce flux direct est remplace par confirm_entry_in_db et confirm_exit_in_db.
    """
    return None


def process_forbidden_vehicle(app, yolo_class: str, site: str | None, guardian_id: int | None) -> None:
    """Log une tentative d'entree de vehicule interdit (poids lourd, bus)."""
    label = yolo_class.upper()
    key = f"FORBIDDEN:{label}"
    now = _now_ts()
    with _lock:
        if now - _last_event.get(key, 0.0) < 15.0:
            return
        _last_event[key] = now

    with app.app_context():
        from models import AccessLog, db, Site
        s_obj = Site.query.filter_by(name=site).first()
        s_id = s_obj.id if s_obj else None

        db.session.add(
            AccessLog(
                plate_number=f"TYPE-{label}",
                action="entry",
                status="forbidden_type",
                site=site,
                site_id=s_id,
                guardian_id=guardian_id,
            )
        )
        db.session.commit()


def check_absence_exits(app) -> int:
    return 0


def init_presence_from_db(app) -> None:
    """Initialise l'etat _present en memoire a partir de la base de donnees au demarrage."""
    with app.app_context():
        from models import AccessLog, db
        from sqlalchemy import func, and_

        sq = db.session.query(
            AccessLog.plate_number.label("plate"),
            func.max(AccessLog.timestamp).label("max_ts"),
        ).group_by(AccessLog.plate_number).subquery()

        q = (
            db.session.query(AccessLog)
            .join(
                sq,
                and_(
                    AccessLog.plate_number == sq.c.plate,
                    AccessLog.timestamp == sq.c.max_ts,
                ),
            )
            .filter(AccessLog.action == "entry")
        )

        with _lock:
            _present.clear()
            for log in q.all():
                _present[log.plate_number] = {
                    "last_seen": time.time() - 3600.0,
                    "site": log.site,
                    "vehicle_id": log.vehicle_id,
                    "status": log.status,
                    "entry_log_id": log.id,
                    "entry_at": log.timestamp,
                }
            print(f"Presence initialisee : {len(_present)} vehicule(s) stationne(s) recharge(s).")


def get_present_plates() -> dict[str, dict]:
    with _lock:
        return dict(_present)


def check_long_stay_violations(app) -> list[dict]:
    """Vehicules presents depuis plus de long_stay_hours."""
    import config

    violations = []
    now = datetime.utcnow()
    with _lock:
        items = list(_present.items())

    for plate, info in items:
        site = info.get("site")
        policy = config.get_site_policy(site)
        limit_h = policy.get("long_stay_hours", 48)
        entry_at = info.get("entry_at")
        if not entry_at:
            continue
        hours = (now - entry_at).total_seconds() / 3600
        if hours >= limit_h:
            violations.append({"plate": plate, "site": site, "hours": round(hours, 1), "info": info})
    return violations
