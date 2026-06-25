from __future__ import annotations

from datetime import datetime, time

import config
from auth import login_required
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from models import db, Site, User, Vehicle, AccessLog, Notification

site_policy_bp = Blueprint("site_policy", __name__)


def _parse_time(s: str) -> time | None:
    try:
        h, m = s.strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def is_within_access_hours(site: str | None, dt: datetime | None = None) -> bool:
    """Verifie si l'heure actuelle est dans les heures d'ouverture autorisees pour le site."""
    policy = config.get_site_policy(site)
    start = _parse_time(policy.get("access_start", "06:00"))
    end = _parse_time(policy.get("access_end", "22:00"))
    if not start or not end:
        return True
    now = dt or datetime.utcnow()
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def max_hours_for_function(site: str | None, function: str | None) -> int:
    """Retourne la duree maximale de stationnement autorisee selon le role."""
    policy = config.get_site_policy(site)
    fn = (function or "").lower()
    if "fonctionnaire" in fn or "administratif" in fn:
        return 24
    if "visiteur" in fn or "visitor" in fn:
        return int(policy.get("max_hours_visitor", 4))
    return int(policy.get("max_hours_student", 8))


@site_policy_bp.route("/admin/politiques-sites", methods=["GET"])
@login_required("admin")
def list_policies():
    """Affiche la liste complete des sites et de leurs politiques."""
    sites = Site.query.order_by(Site.name).all()
    return render_template("admin_site_policies.html", sites=sites)


@site_policy_bp.route("/admin/sites/nouveau", methods=["POST"])
@login_required("admin")
def create_site():
    """Ajoute un nouveau site en base de donnees et met a jour la configuration."""
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").upper().strip()
    try:
        capacity = int(request.form.get("capacity", 50))
        max_student = int(request.form.get("max_hours_student", 8))
        max_visitor = int(request.form.get("max_hours_visitor", 4))
        long_stay = int(request.form.get("long_stay_hours", 48))
    except (ValueError, TypeError):
        flash("Les valeurs numeriques fournies sont invalides.", "danger")
        return redirect(url_for("site_policy.list_policies"))

    access_start = request.form.get("access_start", "06:00").strip()
    access_end = request.form.get("access_end", "22:00").strip()
    camera_entry = request.form.get("camera_url_entry", "").strip()
    camera_exit = request.form.get("camera_url_exit", "").strip()
    gate_ip = request.form.get("gate_ip", "").strip()

    if not name or not code:
        flash("Le nom et le code du site sont obligatoires.", "danger")
        return redirect(url_for("site_policy.list_policies"))

    if Site.query.filter_by(name=name).first() or Site.query.filter_by(code=code).first():
        flash("Un site avec ce nom ou ce code existe deja.", "warning")
        return redirect(url_for("site_policy.list_policies"))

    new_site = Site(
        name=name,
        code=code,
        capacity=capacity,
        camera_url_entry=camera_entry,
        camera_url_exit=camera_exit,
        max_hours_student=max_student,
        max_hours_visitor=max_visitor,
        access_start=access_start,
        access_end=access_end,
        long_stay_hours=long_stay,
        gate_ip=gate_ip or None
    )
    db.session.add(new_site)
    db.session.commit()

    flash(f"Site {name} cree avec succes.", "success")
    return redirect(url_for("site_policy.list_policies"))


@site_policy_bp.route("/admin/sites/modifier/<int:site_id>", methods=["POST"])
@login_required("admin")
def update_site(site_id):
    """Met a jour les informations et politiques d'un site existant."""
    s = Site.query.get_or_404(site_id)
    
    try:
        s.capacity = int(request.form.get("capacity", s.capacity))
        s.max_hours_student = int(request.form.get("max_hours_student", s.max_hours_student))
        s.max_hours_visitor = int(request.form.get("max_hours_visitor", s.max_hours_visitor))
        s.long_stay_hours = int(request.form.get("long_stay_hours", s.long_stay_hours))
    except (ValueError, TypeError):
        flash("Erreur de saisie numerique.", "danger")
        return redirect(url_for("site_policy.list_policies"))

    s.name = request.form.get("name", s.name).strip()
    s.code = request.form.get("code", s.code).upper().strip()
    s.access_start = request.form.get("access_start", s.access_start).strip()
    s.access_end = request.form.get("access_end", s.access_end).strip()
    s.camera_url_entry = request.form.get("camera_url_entry", s.camera_url_entry).strip()
    s.camera_url_exit = request.form.get("camera_url_exit", s.camera_url_exit).strip()
    s.gate_ip = request.form.get("gate_ip", s.gate_ip or "").strip() or None

    db.session.commit()
    flash(f"Site {s.name} mis a jour avec succes.", "success")
    return redirect(url_for("site_policy.list_policies"))


@site_policy_bp.route("/admin/sites/supprimer/<int:site_id>", methods=["POST"])
@login_required("admin")
def delete_site(site_id):
    """Supprime un site de la base de donnees et detache les references."""
    s = Site.query.get_or_404(site_id)
    name = s.name

    # Detacher les utilisateurs, vehicules et logs lies a ce site
    User.query.filter_by(site_id=s.id).update({"site_id": None, "site": None})
    Vehicle.query.filter_by(site_id=s.id).update({"site_id": None, "site_authorized": None})
    AccessLog.query.filter_by(site_id=s.id).update({"site_id": None, "site": None})
    Notification.query.filter_by(site_id=s.id).update({"site_id": None, "site": None})

    db.session.delete(s)
    db.session.commit()

    flash(f"Site {name} supprime avec succes.", "success")
    return redirect(url_for("site_policy.list_policies"))
