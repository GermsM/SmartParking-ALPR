from flask import Blueprint, flash, jsonify, redirect, request, session, url_for
from datetime import datetime

from auth import login_required
from models import DriverAssignment, Vehicle, db, User

drivers_bp = Blueprint("drivers", __name__)


def _get_active_assignment(vehicle_id: int) -> DriverAssignment | None:
    return (
        DriverAssignment.query.filter_by(vehicle_id=vehicle_id, unassigned_at=None)
        .order_by(DriverAssignment.assigned_at.desc())
        .first()
    )


@drivers_bp.route("/api/drivers/assign", methods=["POST"])
@login_required("admin")
def assign_driver():
    data = request.get_json(silent=True) or {}
    vehicle_id = data.get("vehicle_id")
    driver_name = (data.get("driver_name") or "").strip()
    driver_phone = (data.get("driver_phone") or "").strip() or None

    if not vehicle_id or not driver_name:
        return jsonify(error="vehicle_id et driver_name requis"), 400

    vehicle = Vehicle.query.get_or_404(vehicle_id)

    with db.session.begin_nested():
        existing = _get_active_assignment(vehicle_id)
        if existing:
            existing.unassigned_at = datetime.utcnow()
            existing.assigned_by = session.get("user_id")

        assignment = DriverAssignment(
            vehicle_id=vehicle_id,
            driver_name=driver_name,
            driver_phone=driver_phone,
            assigned_by=session.get("user_id"),
            unassigned_at=None,
        )
        db.session.add(assignment)

    db.session.commit()
    return jsonify(ok=True, assignment_id=assignment.id)


@drivers_bp.route("/api/drivers/unassign", methods=["POST"])
@login_required("admin")
def unassign_driver():
    data = request.get_json(silent=True) or {}
    vehicle_id = data.get("vehicle_id")
    if not vehicle_id:
        return jsonify(error="vehicle_id requis"), 400

    assignment = _get_active_assignment(vehicle_id)
    if not assignment:
        return jsonify(error="aucune assignation active"), 404

    assignment.unassigned_at = datetime.utcnow()
    assignment.assigned_by = session.get("user_id")
    db.session.commit()
    return jsonify(ok=True)


@drivers_bp.route("/api/drivers/vehicle/<int:vehicle_id>")
@login_required()
def get_active_driver(vehicle_id):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    role = session.get("role")
    site = session.get("site")

    if role != "admin":
        if vehicle.site_authorized and vehicle.site_authorized != site:
            return jsonify(error="forbidden"), 403
        if vehicle.site_id:
            if str(vehicle.site_id) != str(site):
                return jsonify(error="forbidden"), 403

    assignment = _get_active_assignment(vehicle_id)
    if not assignment:
        return jsonify(active_driver=None)

    result = {
        "id": assignment.id,
        "driver_name": assignment.driver_name,
        "assigned_at": assignment.assigned_at.isoformat() + "Z",
    }

    if role == "admin":
        result["driver_phone"] = assignment.driver_phone

    return jsonify(active_driver=result)


@drivers_bp.route("/api/drivers/history/<int:vehicle_id>")
@login_required()
def get_driver_history(vehicle_id):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    role = session.get("role")
    site = session.get("site")

    if role != "admin":
        if vehicle.site_authorized and vehicle.site_authorized != site:
            return jsonify(error="forbidden"), 403
        if vehicle.site_id:
            if str(vehicle.site_id) != str(site):
                return jsonify(error="forbidden"), 403

    history = (
        DriverAssignment.query.filter_by(vehicle_id=vehicle_id)
        .order_by(DriverAssignment.assigned_at.desc())
        .all()
    )

    out = []
    for h in history:
        item = {
            "id": h.id,
            "driver_name": h.driver_name,
            "assigned_at": h.assigned_at.isoformat() + "Z",
            "unassigned_at": h.unassigned_at.isoformat() + "Z" if h.unassigned_at else None,
        }
        if role == "admin":
            item["driver_phone"] = h.driver_phone
        out.append(item)

    return jsonify(history=out)
