"""Notifications internes (lecture indépendante par utilisateur)."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import and_, exists, not_

from auth import login_required
from models import Notification, NotificationRead, db, User

notifications_bp = Blueprint("notifications", __name__)


def _notifications_query(role: str | None, site: str | None):
    q = Notification.query
    if role == "admin":
        q = q.filter(Notification.category.notin_(["export_reminder", "approve", "ban", "reactivate", "delete"]))
    else:
        user_id = session.get("user_id")
        filters = []
        if site:
            filters.append(Notification.site == site)
        filters.append(Notification.site.is_(None))
        if user_id:
            filters.append(Notification.guardian_id == user_id)
        from sqlalchemy import or_
        q = q.filter(or_(*filters))
    return q


def get_read_ids_for_user(user_id: int) -> set[int]:
    rows = (
        NotificationRead.query.filter_by(user_id=user_id)
        .with_entities(NotificationRead.notification_id)
        .all()
    )
    return {r[0] for r in rows}


def create_notification(
    message: str,
    *,
    site: str | None = None,
    category: str = "system",
    plate_number: str | None = None,
    guardian_id: int | None = None,
    contact_phone: str | None = None,
    whatsapp_message: str | None = None,
) -> Notification:
    n = Notification(
        site=site,
        category=category,
        message=message,
        plate_number=plate_number,
        guardian_id=guardian_id,
        contact_phone=contact_phone,
        whatsapp_message=whatsapp_message,
        is_read=False,
    )
    db.session.add(n)
    db.session.commit()
    return n

def get_notifications_for_user(role: str | None, site: str | None, limit: int = 50) -> list[Notification]:
    return (
        _notifications_query(role, site)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .all()
    )


def count_unread(role: str | None, site: str | None, user_id: int | None) -> int:
    if not user_id:
        return 0
    read_subq = exists().where(
        and_(
            NotificationRead.notification_id == Notification.id,
            NotificationRead.user_id == user_id,
        )
    )
    return _notifications_query(role, site).filter(not_(read_subq)).count()


def mark_notification_read_for_user(notification_id: int, user_id: int) -> None:
    existing = NotificationRead.query.filter_by(
        notification_id=notification_id,
        user_id=user_id,
    ).first()
    if existing:
        return
    db.session.add(NotificationRead(notification_id=notification_id, user_id=user_id))
    db.session.commit()


@notifications_bp.route("/notifications")
@login_required()
def list_notifications():
    user_id = session.get("user_id")
    role = session.get("role")
    site = session.get("site")
    if not site and role != "admin" and user_id:
        user = User.query.get(user_id)
        if user and user.site:
            session["site"] = user.site
            session.modified = True
            site = user.site
    items = get_notifications_for_user(role, site)
    read_ids = get_read_ids_for_user(user_id) if user_id else set()
    return render_template("notifications.html", notifications=items, read_ids=read_ids)


@notifications_bp.route("/api/notifications/unread-count")
@login_required()
def api_unread_count():
    role = session.get("role")
    site = session.get("site")
    if not site and role != "admin" and session.get("user_id"):
        user = User.query.get(session["user_id"])
        if user and user.site:
            session["site"] = user.site
            session.modified = True
            site = user.site
    return jsonify(
        count=count_unread(role, site, session.get("user_id"))
    )


def _user_can_access_notification(n: Notification) -> bool:
    if session.get("role") == "admin":
        return True
    site = session.get("site")
    if not site and session.get("user_id"):
        user = User.query.get(session["user_id"])
        if user and user.site:
            session["site"] = user.site
            session.modified = True
            site = user.site
    if not n.site:
        return True
    return n.site == site


@notifications_bp.route("/notifications/<int:nid>/read", methods=["POST"])
@login_required()
def mark_read(nid):
    n = Notification.query.get_or_404(nid)
    if not _user_can_access_notification(n):
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        if wants_json:
            return jsonify(ok=False, error="forbidden"), 403
        flash("Notification inaccessible.", "warning")
        return redirect(url_for("notifications.list_notifications"))
    mark_notification_read_for_user(nid, session["user_id"])
    wants_json = (
        request.headers.get("X-Requested-With") == "fetch"
        or "application/json" in (request.headers.get("Accept") or "")
    )
    if wants_json:
        unread = count_unread(session.get("role"), session.get("site"), session.get("user_id"))
        return jsonify(ok=True, unread_count=unread)
    flash("Notification marquée comme lue.", "success")
    return redirect(url_for("notifications.list_notifications"))


@notifications_bp.route("/notifications/<int:nid>/supprimer", methods=["POST"])
@login_required()
def delete_notification(nid):
    n = Notification.query.get_or_404(nid)
    if not _user_can_access_notification(n):
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        if wants_json:
            return jsonify(ok=False, error="forbidden"), 403
        flash("Vous ne pouvez supprimer que les notifications de votre site.", "warning")
        return redirect(url_for("notifications.list_notifications"))
    try:
        NotificationRead.query.filter_by(notification_id=nid).delete()
        db.session.delete(n)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        if wants_json:
            return jsonify(ok=False, error=str(exc)), 500
        flash("Impossible de supprimer cette notification.", "danger")
        return redirect(url_for("notifications.list_notifications"))
    wants_json = (
        request.headers.get("X-Requested-With") == "fetch"
        or "application/json" in (request.headers.get("Accept") or "")
    )
    if wants_json:
        unread = count_unread(session.get("role"), session.get("site"), session.get("user_id"))
        return jsonify(ok=True, unread_count=unread)
    flash("Notification supprimée.", "success")
    return redirect(url_for("notifications.list_notifications"))


@notifications_bp.route("/notifications/clear-all", methods=["POST"])
@login_required()
def clear_all_notifications():
    role = session.get("role")
    site = session.get("site")
    if not site and role != "admin" and session.get("user_id"):
        user = User.query.get(session["user_id"])
        if user and user.site:
            session["site"] = user.site
            session.modified = True
            site = user.site
    
    # We want to clear notifications the user can see.
    items = get_notifications_for_user(role, site, limit=1000)
    
    deleted_count = 0
    try:
        for n in items:
            if _user_can_access_notification(n):
                NotificationRead.query.filter_by(notification_id=n.id).delete()
                db.session.delete(n)
                deleted_count += 1
        db.session.commit()
        flash(f"{deleted_count} notification(s) supprimée(s).", "success")
    except Exception as exc:
        db.session.rollback()
        flash("Une erreur est survenue lors de la suppression des notifications.", "danger")
        
    return redirect(url_for("notifications.list_notifications"))


def maybe_create_export_reminder(role: str | None, site: str | None) -> None:
    if role != "gardien" or not site:
        return
    today = datetime.utcnow().date()
    existing = (
        Notification.query.filter_by(category="export_reminder", site=site)
        .filter(Notification.created_at >= datetime.combine(today, datetime.min.time()))
        .first()
    )
    if existing:
        return
    if datetime.now().hour >= 17:
        create_notification(
            "Rappel : exportez l'historique des mouvements du jour avant la fin de votre quart.",
            site=site,
            category="export_reminder",
        )
