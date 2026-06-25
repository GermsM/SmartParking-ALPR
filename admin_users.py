import secrets
import string

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from auth import login_required
from models import AccessLog, Notification, User, db

admin_bp = Blueprint("admin_staff", __name__)


def _random_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@admin_bp.route("/admin/gardiens")
@login_required("admin")
def list_gardiens():
    gardiens = User.query.filter_by(role="gardien").order_by(User.username).all()
    return render_template("admin_gardiens.html", gardiens=gardiens)


@admin_bp.route("/admin/gardiens/nouveau", methods=["POST"])
@login_required("admin")
def create_gardien():
    username = (request.form.get("username") or "").strip().lower()
    full_name = (request.form.get("full_name") or "").strip() or None
    site = (request.form.get("site") or "").strip()
    auto_password = request.form.get("auto_password") == "on"
    password = (request.form.get("password") or "").strip()

    if auto_password or not password:
        password = _random_password()

    if not username or not site:
        flash("Identifiant et site sont obligatoires.", "danger")
        return redirect(url_for("admin_staff.list_gardiens"))
    if len(password) < 4:
        flash("Mot de passe trop court (minimum 4 caractères).", "warning")
        return redirect(url_for("admin_staff.list_gardiens"))
    if User.query.filter_by(username=username).first():
        flash("Ce nom d'utilisateur existe déjà.", "warning")
        return redirect(url_for("admin_staff.list_gardiens"))

    u = User(
        username=username,
        password=generate_password_hash(password),
        role="gardien",
        full_name=full_name,
        site=site,
        is_active=True,
        must_change_password=True,
    )
    db.session.add(u)
    db.session.commit()
    flash(
        f"Compte gardien « {username} » créé. Mot de passe temporaire : {password} "
        f"(à changer à la première connexion).",
        "success",
    )
    return redirect(url_for("admin_staff.list_gardiens"))


@admin_bp.route("/admin/gardiens/<int:user_id>/toggle-actif", methods=["POST"])
@login_required("admin")
def toggle_gardien_active(user_id):
    u = User.query.get_or_404(user_id)
    if u.role != "gardien":
        flash("Seuls les comptes gardiens peuvent être modifiés ici.", "danger")
        return redirect(url_for("admin_staff.list_gardiens"))
    u.is_active = not bool(u.is_active)
    db.session.commit()
    flash(
        f"Compte « {u.username} » : {'activé' if u.is_active else 'désactivé'}.",
        "success",
    )
    return redirect(url_for("admin_staff.list_gardiens"))


@admin_bp.route("/admin/gardiens/<int:user_id>/mot-de-passe", methods=["POST"])
@login_required("admin")
def reset_gardien_password(user_id):
    u = User.query.get_or_404(user_id)
    if u.role != "gardien":
        flash("Seuls les comptes gardiens.", "danger")
        return redirect(url_for("admin_staff.list_gardiens"))
    password = (request.form.get("new_password") or "").strip()
    if not password:
        password = _random_password()
    if len(password) < 4:
        flash("Mot de passe trop court.", "warning")
        return redirect(url_for("admin_staff.list_gardiens"))
    u.password = generate_password_hash(password)
    u.must_change_password = True
    db.session.commit()
    flash(f"Mot de passe réinitialisé pour « {u.username} » : {password}", "success")
    return redirect(url_for("admin_staff.list_gardiens"))


@admin_bp.route("/admin/gardiens/<int:user_id>/modifier", methods=["POST"])
@login_required("admin")
def update_gardien(user_id):
    """Modifie le nom affiche et le site d'un gardien existant."""
    u = User.query.get_or_404(user_id)
    if u.role != "gardien":
        flash("Seuls les comptes gardiens peuvent etre modifies ici.", "danger")
        return redirect(url_for("admin_staff.list_gardiens"))
    u.full_name = request.form.get("full_name", "").strip() or None
    u.site = request.form.get("site", "").strip() or None
    db.session.commit()
    flash(f"Compte « {u.username} » mis a jour.", "success")
    return redirect(url_for("admin_staff.list_gardiens"))


@admin_bp.route("/admin/gardiens/<int:user_id>/supprimer", methods=["POST"])
@login_required("admin")
def delete_gardien(user_id):
    u = User.query.get_or_404(user_id)
    if u.role != "gardien":
        flash("Seuls les comptes gardiens peuvent être supprimés ici.", "danger")
        return redirect(url_for("admin_staff.list_gardiens"))

    AccessLog.query.filter_by(guardian_id=u.id).update({"guardian_id": None})
    Notification.query.filter_by(guardian_id=u.id).update({"guardian_id": None})
    db.session.delete(u)
    db.session.commit()
    flash(f"Compte gardien « {u.username} » supprimé.", "success")
    return redirect(url_for("admin_staff.list_gardiens"))
