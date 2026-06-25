from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

from models import db, User, Site

DEFAULT_SITE_NAME = "Msr Mulindwa"


def migrate_sqlite_schema(app) -> None:
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not uri.startswith("sqlite"):
        return

    engine = db.engine

    def add_col(table: str, col: str, ddl: str) -> None:
        insp = inspect(engine)
        if table not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns(table)}
        if col in existing:
            return
        with engine.connect() as conn:
            conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {col} {ddl}'))
            conn.commit()

    # Colonnes de retrocompatibilite existantes
    add_col("vehicle", "created_by", "INTEGER")
    add_col("vehicle", "created_at", "DATETIME")
    add_col("vehicle", "function", "VARCHAR(100)")
    add_col("vehicle", "site_authorized", "VARCHAR(50)")
    add_col("vehicle", "status", "VARCHAR(20)")
    add_col("vehicle", "owner_phone", "VARCHAR(30)")
    add_col("vehicle", "owner_email", "VARCHAR(120)")
    add_col("vehicle", "owner_address", "VARCHAR(255)")
    add_col("vehicle", "vehicle_model", "VARCHAR(120)")

    add_col("access_log", "vehicle_id", "INTEGER")
    add_col("access_log", "guardian_id", "INTEGER")
    add_col("access_log", "duration_minutes", "INTEGER")

    add_col("user", "full_name", "VARCHAR(100)")
    add_col("user", "site", "VARCHAR(50)")
    add_col("user", "is_active", "INTEGER")
    add_col("user", "must_change_password", "INTEGER")

    add_col("notification", "contact_phone", "VARCHAR(30)")
    add_col("notification", "whatsapp_message", "VARCHAR(1000)")

    add_col("vehicle", "vehicle_type", "VARCHAR(30)")

    # Adresse IP du portail physique pour chaque site
    add_col("site", "gate_ip", "VARCHAR(255)")

    # Nouvelles colonnes de site_id pour la modularite
    add_col("user", "site_id", "INTEGER")
    add_col("vehicle", "site_id", "INTEGER")
    add_col("access_log", "site_id", "INTEGER")
    add_col("notification", "site_id", "INTEGER")

    with app.app_context():
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE user SET is_active = 1 WHERE is_active IS NULL"))
            conn.commit()


def seed_default_site_if_empty() -> None:
    if Site.query.count() > 0:
        return
    default = Site(
        name=DEFAULT_SITE_NAME,
        code=DEFAULT_SITE_NAME[:4].upper(),
        capacity=50,
        max_hours_student=8,
        max_hours_visitor=4,
        access_start="06:00",
        access_end="22:00",
        long_stay_hours=48,
    )
    db.session.add(default)
    db.session.commit()
    print(f"Site par defaut cree : {DEFAULT_SITE_NAME}")


def seed_default_users_if_empty() -> None:
    if User.query.count() > 0:
        return

    default_site = Site.query.filter_by(name=DEFAULT_SITE_NAME).first()

    db.session.add(
        User(
            username="admin",
            password=generate_password_hash("admin123"),
            role="admin",
            full_name="Administrateur",
            site=None,
            site_id=None,
            is_active=True,
        )
    )
    db.session.add(
        User(
            username="gardien",
            password=generate_password_hash("gardien123"),
            role="gardien",
            full_name="Gardien site",
            site=DEFAULT_SITE_NAME,
            site_id=default_site.id if default_site else None,
            is_active=True,
        )
    )
    db.session.commit()
    print("Comptes initiaux crees - admin / admin123 , gardien / gardien123")


def init_app_database(app) -> None:
    with app.app_context():
        db.create_all()
        migrate_sqlite_schema(app)
        seed_default_site_if_empty()
        seed_default_users_if_empty()
