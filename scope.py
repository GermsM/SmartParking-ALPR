"""Filtres de périmètre site — même logique pour tableau de bord, listes, exports."""
from __future__ import annotations

from models import Vehicle


def vehicles_for_user(role: str | None, site: str | None, *, status: str | None = None):
    """Requête véhicules selon le rôle. Gardien = site exact ou « tous sites » (site_authorized est None)."""
    q = Vehicle.query
    if role != "admin" and site:
        from sqlalchemy import or_
        q = q.filter(or_(Vehicle.site_authorized == site, Vehicle.site_authorized.is_(None)))
    if status:
        q = q.filter(Vehicle.status == status)
    return q
