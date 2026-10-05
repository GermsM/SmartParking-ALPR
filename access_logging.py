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


def manual_access_in_db(app, plate: str, direction: str, site: str | None, guardian_id: int | None) -> dict:
    """Enregistre un acces manuel saisi par le gardien (OCR absent ou echoue).

    Cree un AccessLog dont le status est ``"manual"`` — ce statut esttraite comme legitime (non alerte)
    par les indicateurs du tableau de bord. La plaque est cherchee dans le registre
    des vehicules afin de fournir au gardien le statut (authorise / inconnu / banni),
    de la meme facon qu'une detection automatique.
    """
    plate = (plate or "").upper().strip()
    direction = (direction or "entry").lower()
    if direction not in ("entry", "exit"):
        direction = "entry"

    status, vehicle_id, site_auth, vehicle = lookup_vehicle_status(app, plate)

    with app.app_context():
        from models import AccessLog, db, Site

        s_obj = Site.query.filter_by(name=site).first()
        s_id = s_obj.id if s_obj else None

        log = AccessLog(
            plate_number=plate,
            vehicle_id=vehicle_id,
            action=direction,
            status="manual",
            site=site,
            site_id=s_id,
            guardian_id=guardian_id,
        )
        db.session.add(log)
        db.session.commit()
        entry_at = log.timestamp

    now = _now_ts()
    with _lock:
        if direction == "entry":
            _present[plate] = {
                "last_seen": now,
                "site": site,
                "vehicle_id": vehicle_id,
                "status": status,
                "entry_log_id": log.id,
                "entry_at": entry_at,
            }
        else:
            _present.pop(plate, None)
        _last_event[plate] = now

    print(f"[ACCES] Acces manuel {direction} pour la plaque {plate} sur le site {site} — statut registre: {status}")

    return {
        "plate": plate,
        "direction": direction,
        "registry_status": status,
        "vehicle_id": vehicle_id,
        "owner_name": vehicle.owner_name if vehicle else None,
        "owner_phone": vehicle.owner_phone if vehicle else None,
        "owner_email": vehicle.owner_email if vehicle else None,
        "site_authorized": site_auth,
    }


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


def init_presence_from_db(app) -> None:
    """Initialise l'etat _present en memoire a partir de la base de donnees au demarrage."""
    with app.app_context():
        from models import AccessLog, db, Vehicle
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
            # Les entrees refusees (banni, type interdit) ne stationnent pas :
            # elles ne doivent pas etre rechargees comme presentes au demarrage.
            for log in q.filter(AccessLog.status.notin_(("banned", "forbidden_type"))).all():
                # Ignorer aussi les plaques qui n'existent plus dans le registre
                # ou qui ne sont pas actives : pas de surveillance dormeur dessus.
                v = Vehicle.query.filter_by(plate_number=log.plate_number).first()
                if not v or v.status != "active":
                    continue
                _present[log.plate_number] = {
                    "last_seen": time.time() - 3600.0,
                    "site": log.site,
                    "vehicle_id": log.vehicle_id,
                    "status": log.status,
                    "entry_log_id": log.id,
                    "entry_at": log.timestamp,
                }
            print(f"Presence initialisee : {len(_present)} vehicule(s) stationne(s) recharge(s).")


def remove_present_plate(plate: str) -> bool:
    """Retire une plaque de l'etat de presence en memoire.

    Appele a la suppression d'un vehicule du registre : une plaque qui
    n'existe plus en base ne doit plus etre consideree comme stationnee
    et ne doit donc plus declencher d'alertes (dormeur, etc.).
    """
    plate = (plate or "").upper().strip()
    if not plate:
        return False
    with _lock:
        removed = _present.pop(plate, None) is not None
    if removed:
        print(f"[ACCES] Presence purgee pour la plaque {plate} (vehicule supprime du registre)")
    return removed


def get_present_plates() -> dict[str, dict]:
    with _lock:
        return dict(_present)


def check_long_stay_violations(app) -> list[dict]:
    """Vehicules presents depuis plus de long_stay_hours.

    Seuls les vehicules toujours enregistres et ACTIFS dans la base
    doivent generer une alerte : une plaque supprimee, bannie ou en
    attente du registre n'est plus sous surveillance dormeur.
    """
    import config

    violations = []
    now = datetime.utcnow()
    with _lock:
        items = list(_present.items())

    if not items:
        return violations

    # Resoudre le statut actuel en base pour toutes les plaques presentes.
    from models import Vehicle
    plates = [plate for plate, _ in items]
    with app.app_context():
        rows = Vehicle.query.filter(Vehicle.plate_number.in_(plates)).all()
        status_by_plate = {v.plate_number: v.status for v in rows}

    for plate, info in items:
        # Vehicule supprime, banni ou en attente : pas d'alerte dormeur.
        if status_by_plate.get(plate) != "active":
            continue
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
