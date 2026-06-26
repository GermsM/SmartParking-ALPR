from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from auth import login_required
from models import Vehicle, db
from email_service import (
    notify_owner_banned,
    notify_owner_profile_updated,
    notify_owner_reactivated,
    notify_owner_registration,
)
from notifications import create_notification
from scope import vehicles_for_user
from security_alerts import invalidate_banned_vehicle_cache
import config

vehicles_bp = Blueprint("vehicles", __name__)


def _invalidate_plates():
    invalidate_banned_vehicle_cache()


def _vehicle_extra_from_form():
    return {
        "owner_phone": request.form.get("owner_phone", "").strip() or None,
        "owner_email": request.form.get("owner_email", "").strip() or None,
        "owner_address": request.form.get("owner_address", "").strip() or None,
        "vehicle_model": request.form.get("vehicle_model", "").strip() or None,
        "vehicle_type": request.form.get("vehicle_type", "auto").strip() or "auto",
    }


def _generate_internal_plate(site: str | None) -> str:
    site_cfg = config.SITE_CONFIG.get(site or "", {})
    code = site_cfg.get("code", "UCB")
    count = Vehicle.query.filter(Vehicle.plate_number.like(f"UCB-{code}-%")).count()
    return f"UCB-{code}-{count + 1:04d}"


def _validate_vehicle_type(vehicle_type: str) -> str | None:
    vt = (vehicle_type or "auto").lower()
    if vt in config.FORBIDDEN_VEHICLE_TYPES:
        return f"Type « {vt} » interdit sur le campus (camping-cars, poids lourds, bus)."
    return None


def _email_owner_registration(vehicle: Vehicle, *, pending: bool) -> None:
    if not vehicle.owner_email:
        return
    notify_owner_registration(
        owner_name=vehicle.owner_name,
        owner_email=vehicle.owner_email,
        plate=vehicle.plate_number,
        site=vehicle.site_authorized,
        status=vehicle.status,
        vehicle_model=vehicle.vehicle_model,
        pending=pending,
    )
    flash(f"Un e-mail de confirmation sera envoyé à {vehicle.owner_email} (en arrière-plan).", "success")


def _email_owner_banned(vehicle: Vehicle, reason: str = "décision administrative") -> None:
    if not vehicle.owner_email:
        return
    notify_owner_banned(
        owner_name=vehicle.owner_name,
        owner_email=vehicle.owner_email,
        plate=vehicle.plate_number,
        site=vehicle.site_authorized,
        reason=reason,
    )
    flash(f"Un e-mail de suspension sera envoyé à {vehicle.owner_email} (en arrière-plan).", "info")


def _email_owner_reactivated(vehicle: Vehicle) -> None:
    if not vehicle.owner_email:
        return
    notify_owner_reactivated(
        owner_name=vehicle.owner_name,
        owner_email=vehicle.owner_email,
        plate=vehicle.plate_number,
        site=vehicle.site_authorized,
    )
    flash(f"Un e-mail de réactivation sera envoyé à {vehicle.owner_email} (en arrière-plan).", "info")


def _email_owner_updated(vehicle: Vehicle, changes: list[str]) -> None:
    if not vehicle.owner_email or not changes:
        return
    notify_owner_profile_updated(
        owner_name=vehicle.owner_name,
        owner_email=vehicle.owner_email,
        plate=vehicle.plate_number,
        site=vehicle.site_authorized,
        changes=changes,
    )
    flash(f"Un e-mail de mise à jour de profil sera envoyé à {vehicle.owner_email} (en arrière-plan).", "info")


@vehicles_bp.route("/vehicles")
@login_required()
def list_vehicles():
    role = session.get("role")
    site = session.get("site")
    if role == "admin":
        vehicles = Vehicle.query.order_by(Vehicle.created_at.desc()).all()
    else:
        vehicles = (
            vehicles_for_user(role, site)
            .order_by(Vehicle.created_at.desc())
            .all()
        )
    return render_template("vehicles.html", vehicles=vehicles)


@vehicles_bp.route("/vehicles/add", methods=["GET", "POST"])
@login_required()
def add_vehicle():
    if request.method == "POST":
        no_plate = request.form.get("no_plate") == "on"
        vehicle_type = request.form.get("vehicle_type", "auto").strip() or "auto"

        err = _validate_vehicle_type(vehicle_type)
        if err:
            flash(err, "danger")
            return redirect(url_for("vehicles.add_vehicle"))

        if no_plate or vehicle_type == "sans_plaque":
            site_auth = session.get("site") if session.get("role") == "gardien" else request.form.get("site_authorized")
            plate = _generate_internal_plate(site_auth)
            vehicle_type = "sans_plaque"
        else:
            plate = (request.form.get("plate_number") or "").upper().strip()
            if not plate:
                flash("La plaque est obligatoire (ou cochez « sans plaque »).", "warning")
                return redirect(url_for("vehicles.add_vehicle"))

        if Vehicle.query.filter_by(plate_number=plate).first():
            flash("Ce véhicule existe déjà !", "warning")
        else:
            if session.get("role") == "gardien":
                site_auth = session.get("site")
                if not site_auth:
                    flash("Votre compte n'a pas de site assigné. Contactez l'administrateur.", "danger")
                    return redirect(url_for("vehicles.add_vehicle"))
                status = "pending"
            else:
                site_auth = request.form.get("site_authorized") or None
                status = "active"

            extra = _vehicle_extra_from_form()
            vehicle = Vehicle(
                plate_number=plate,
                owner_name=request.form["owner_name"].strip(),
                function=request.form.get("function", "").strip() or None,
                site_authorized=site_auth,
                status=status,
                created_by=session.get("user_id"),
                vehicle_type=vehicle_type,
                **{k: v for k, v in extra.items() if k != "vehicle_type"},
            )
            db.session.add(vehicle)
            db.session.commit()
            _invalidate_plates()
            if vehicle.owner_email:
                _email_owner_registration(vehicle, pending=(status == "pending"))
            elif session.get("role") == "admin":
                flash(
                    "Conseil : renseignez l'e-mail du propriétaire pour lui envoyer une confirmation automatique.",
                    "info",
                )
            if no_plate or vehicle_type == "sans_plaque":
                flash(
                    f"Véhicule sans plaque enregistré avec identifiant UCB : {plate}. "
                    f"Collez cet code sur le véhicule (autocollant / QR).",
                    "success",
                )
            elif session.get("role") == "gardien":
                flash("Demande enregistrée : en attente de validation par l'administrateur.", "success")
            else:
                flash("Véhicule ajouté avec succès !", "success")
            return redirect(url_for("vehicles.list_vehicles"))

    return render_template("add_vehicle.html")


@vehicles_bp.route("/vehicles/validate/<int:id>", methods=["POST"])
@login_required("admin")
def validate_vehicle(id):
    vehicle = Vehicle.query.get_or_404(id)
    action = request.form.get("action")

    if action == "approve":
        vehicle.status = "active"
        flash(f"Véhicule {vehicle.plate_number} approuvé avec succès !", "success")
        if vehicle.owner_email:
            _email_owner_registration(vehicle, pending=False)
        create_notification(
            f"L'administrateur a approuvé le véhicule {vehicle.plate_number}.",
            site=vehicle.site_authorized,
            category="approve",
            plate_number=vehicle.plate_number,
            guardian_id=vehicle.created_by,
        )
    elif action == "ban":
        vehicle.status = "banned"
        flash(f"Véhicule {vehicle.plate_number} interdit !", "danger")
        _email_owner_banned(vehicle, reason="demande refusée par l'administration")
        create_notification(
            f"L'administrateur a refusé et banni le véhicule {vehicle.plate_number}.",
            site=vehicle.site_authorized,
            category="ban",
            plate_number=vehicle.plate_number,
            guardian_id=vehicle.created_by,
        )

    db.session.commit()
    _invalidate_plates()
    return redirect(url_for("vehicles.list_vehicles"))


@vehicles_bp.route("/vehicles/ban/<int:id>", methods=["POST"])
@login_required("admin")
def ban_vehicle(id):
    vehicle = Vehicle.query.get_or_404(id)
    if vehicle.status == "banned":
        flash("Ce véhicule est déjà interdit.", "info")
        return redirect(url_for("vehicles.list_vehicles"))
    vehicle.status = "banned"
    db.session.commit()
    _invalidate_plates()
    _email_owner_banned(vehicle)
    create_notification(
        f"Le véhicule {vehicle.plate_number} a été banni par l'administrateur.",
        site=vehicle.site_authorized,
        category="ban",
        plate_number=vehicle.plate_number,
        guardian_id=vehicle.created_by,
    )
    flash(f"Véhicule {vehicle.plate_number} interdit (banni).", "danger")
    return redirect(url_for("vehicles.list_vehicles"))


@vehicles_bp.route("/vehicles/reactivate/<int:id>", methods=["POST"])
@login_required("admin")
def reactivate_vehicle(id):
    vehicle = Vehicle.query.get_or_404(id)
    if vehicle.status != "banned":
        flash("Seuls les véhicules bannis peuvent être réactivés.", "warning")
        return redirect(url_for("vehicles.list_vehicles"))
    vehicle.status = "active"
    db.session.commit()
    _invalidate_plates()
    _email_owner_reactivated(vehicle)
    create_notification(
        f"Le véhicule {vehicle.plate_number} a été réactivé par l'administrateur (accès autorisé).",
        site=vehicle.site_authorized,
        category="reactivate",
        plate_number=vehicle.plate_number,
        guardian_id=vehicle.created_by,
    )
    flash(f"Véhicule {vehicle.plate_number} réactivé avec succès.", "success")
    return redirect(url_for("vehicles.list_vehicles"))


@vehicles_bp.route("/vehicles/delete/<int:id>", methods=["POST"])
@login_required("admin")
def delete_vehicle(id):
    vehicle = Vehicle.query.get_or_404(id)
    plate = vehicle.plate_number
    site = vehicle.site_authorized
    creator = vehicle.created_by
    db.session.delete(vehicle)
    db.session.commit()
    _invalidate_plates()
    create_notification(
        f"Le véhicule {plate} a été supprimé du registre par l'administrateur.",
        site=site,
        category="delete",
        plate_number=plate,
        guardian_id=creator,
    )
    flash("Véhicule supprimé.", "success")
    return redirect(url_for("vehicles.list_vehicles"))


@vehicles_bp.route("/vehicles/edit/<int:id>", methods=["GET", "POST"])
@login_required("admin")
def edit_vehicle(id):
    vehicle = Vehicle.query.get_or_404(id)
    if request.method == "POST":
        old_status = vehicle.status
        old = {
            "owner_name": vehicle.owner_name,
            "owner_phone": vehicle.owner_phone,
            "owner_email": vehicle.owner_email,
            "owner_address": vehicle.owner_address,
            "vehicle_model": vehicle.vehicle_model,
            "function": vehicle.function,
            "site_authorized": vehicle.site_authorized,
            "status": vehicle.status,
            "vehicle_type": vehicle.vehicle_type,
        }
        vehicle.owner_name = request.form["owner_name"].strip()
        vehicle.function = request.form.get("function", "").strip() or None
        site = request.form.get("site_authorized", "").strip()
        vehicle.site_authorized = site or None
        vehicle.status = request.form.get("status", vehicle.status)
        vehicle.vehicle_type = request.form.get("vehicle_type", vehicle.vehicle_type or "auto")
        extra = _vehicle_extra_from_form()
        vehicle.owner_phone = extra["owner_phone"]
        vehicle.owner_email = extra["owner_email"]
        vehicle.owner_address = extra["owner_address"]
        vehicle.vehicle_model = extra["vehicle_model"]
        db.session.commit()
        _invalidate_plates()

        changes = []
        labels = {
            "owner_name": "Nom du propriétaire",
            "owner_phone": "Téléphone",
            "owner_email": "E-mail",
            "owner_address": "Adresse",
            "vehicle_model": "Modèle",
            "function": "Fonction",
            "site_authorized": "Site autorisé",
            "status": "Statut",
            "vehicle_type": "Type de véhicule",
        }
        for key, label in labels.items():
            if old.get(key) != getattr(vehicle, key):
                changes.append(f"{label}")

        email_target = vehicle.owner_email or old.get("owner_email")
        if changes and email_target:
            notify_owner_profile_updated(
                owner_name=vehicle.owner_name,
                owner_email=email_target,
                plate=vehicle.plate_number,
                site=vehicle.site_authorized,
                changes=changes,
            )
            flash(f"Un e-mail de mise à jour sera envoyé à {email_target} (en arrière-plan).", "info")

        if old_status != vehicle.status:
            create_notification(
                f"Statut du véhicule {vehicle.plate_number} modifié : {old_status} → {vehicle.status}.",
                site=vehicle.site_authorized,
                category="status_change",
                plate_number=vehicle.plate_number,
                guardian_id=vehicle.created_by,
            )
            if vehicle.status == "banned" and old_status != "banned":
                _email_owner_banned(vehicle)
            elif vehicle.status == "active" and old_status == "banned":
                _email_owner_reactivated(vehicle)

        flash("Véhicule mis à jour.", "success")
        return redirect(url_for("vehicles.list_vehicles"))
    return render_template("edit_vehicle.html", vehicle=vehicle)
