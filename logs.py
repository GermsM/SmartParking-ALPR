import csv
from datetime import datetime, timedelta
from io import StringIO

from flask import Blueprint, Response, render_template, request, session

import config
from auth import login_required
from frequency_export import pair_access_logs
from models import AccessLog, User
from site_policies import max_hours_for_function

logs_bp = Blueprint("logs", __name__)


@logs_bp.route("/logs")
@login_required()
def view_logs():
    if session.get("role") == "admin":
        logs = AccessLog.query.order_by(AccessLog.timestamp.desc()).limit(100).all()
        site_policy = None
    else:
        site = session.get("site")
        logs = (
            AccessLog.query.filter_by(site=site)
            .order_by(AccessLog.timestamp.desc())
            .limit(50)
            .all()
        )
        site_policy = config.get_site_policy(site)

    return render_template("logs.html", logs=logs, site_policy=site_policy)


@logs_bp.route("/logs/export")
@login_required()
def export_frequency_page():
    today = datetime.utcnow().date()
    default_from = (today - timedelta(days=30)).isoformat()
    default_to = today.isoformat()
    return render_template(
        "export_frequency.html",
        default_from=default_from,
        default_to=default_to,
    )


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d")
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return None


def _build_guardian_map(logs: list[AccessLog]) -> dict[int, str]:
    ids = {log.guardian_id for log in logs if log.guardian_id}
    if not ids:
        return {}
    users = User.query.filter(User.id.in_(ids)).all()
    return {u.id: (u.full_name or u.username) for u in users}


@logs_bp.route("/logs/export.csv")
@login_required()
def export_frequency_csv():
    raw_from = request.args.get("date_from")
    raw_to = request.args.get("date_to")
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    dt_to = _parse_date(raw_to) or today
    dt_from = _parse_date(raw_from) or (dt_to - timedelta(days=30))

    if dt_from > dt_to:
        dt_from, dt_to = dt_to, dt_from

    end_exclusive = dt_to + timedelta(days=1)

    q = AccessLog.query.filter(
        AccessLog.timestamp >= dt_from,
        AccessLog.timestamp < end_exclusive,
    ).order_by(AccessLog.timestamp.asc())

    if session.get("role") != "admin":
        site = session.get("site")
        if site:
            q = q.filter(AccessLog.site == site)

    logs = q.all()
    guardian_map = _build_guardian_map(logs)
    visits = pair_access_logs(logs, guardian_map)

    buf = StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(
        [
            "Plaque",
            "Site",
            "Date arrivée",
            "Heure arrivée",
            "Date départ",
            "Heure départ",
            "Durée (minutes)",
            "Durée (texte)",
            "Gardien (jour)",
            "Statut entrée",
            "Statut sortie",
            "Remarque",
        ]
    )

    for v in visits:
        de = v.entry_at
        ds = v.exit_at
        writer.writerow(
            [
                v.plate,
                v.site or "",
                de.strftime("%Y-%m-%d") if de else "",
                de.strftime("%H:%M:%S") if de else "",
                ds.strftime("%Y-%m-%d") if ds else "",
                ds.strftime("%H:%M:%S") if ds else "",
                v.duration_minutes if v.duration_minutes is not None else "",
                _duration_text(v.duration_minutes) if v.duration_minutes is not None else "",
                v.guardian_name or "",
                v.entry_status or "",
                v.exit_status or "",
                v.remark or "",
            ]
        )

    fn_from = dt_from.strftime("%Y%m%d")
    fn_to = (end_exclusive - timedelta(days=1)).strftime("%Y%m%d")
    filename = f"frequences_parking_{fn_from}_{fn_to}.csv"

    payload = "\ufeff" + buf.getvalue()
    return Response(
        payload.encode("utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _duration_text(minutes: int) -> str:
    h, m = divmod(max(0, int(minutes)), 60)
    return f"{h}h{m:02d}"
