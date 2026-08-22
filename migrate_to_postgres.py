"""
Migration automatique SQLite -> PostgreSQL pour le système de gestion des parkings UCB.

Ce script :
  1. Lit toutes les données depuis la base SQLite locale (parking.db).
  2. Crée le schéma complet dans PostgreSQL (db.create_all).
  3. Transfère Sites, Users, Vehicles, AccessLogs, Notifications, NotificationReads.
  4. Conserve les correspondances (site_id, vehicle_id, guardian_id) car les IDs sont réutilisés.

Pré-requis :
  - pip install psycopg2-binary
  - Une base PostgreSQL vide (ex: createdb parking_ucb)
  - Définir DATABASE_URL, ex:
      export DATABASE_URL="postgresql://user:password@localhost:5432/parking_ucb"
  - Lancer :  python migrate_to_postgres.py

Aucune modification manuelle n'est nécessaire : le script fait tout.
"""
from __future__ import annotations

import os

# 1) Resoudre l'URL PostgreSQL avant tout import de l'app.
#    Priorite : variable d'environnement DATABASE_URL, sinon config.yaml (database_url).
PG_URL = os.environ.get("DATABASE_URL")
if not PG_URL or not PG_URL.startswith("postgresql"):
    try:
        import yaml as _yaml
        with open("config.yaml", "r", encoding="utf-8") as _f:
            _cfg = _yaml.safe_load(_f) or {}
        PG_URL = _cfg.get("database_url")
    except Exception:
        PG_URL = None

if not PG_URL or not PG_URL.startswith("postgresql"):
    raise SystemExit(
        "Aucune URL PostgreSQL trouvee.\n"
        "Definissez DATABASE_URL, ex:\n"
        "  $env:DATABASE_URL=\"postgresql://user:pass@localhost:5432/parking_ucb\"\n"
        "ou renseignez 'database_url' dans config.yaml, puis relancez ce script."
    )

import app as a
from models import db, Site, User, Vehicle, AccessLog, Notification, NotificationRead

SQLITE_BIND = "sqlite"
PG_BIND = "postgres"


def _dump_sqlite_rows(model):
    """Lit les lignes depuis la base SQLite via un moteur dedie (sans toucher a la config Flask)."""
    from sqlalchemy import create_engine, inspect as sa_inspect
    # Réutilise le chemin SQLite par défaut du projet
    sqlite_uri = "sqlite:///parking.db"
    try:
        eng = create_engine(sqlite_uri)
        insp = sa_inspect(eng)
        if model.__tablename__ not in insp.get_table_names():
            return []
        with eng.connect() as conn:
            rows = list(conn.execute(__import__("sqlalchemy").text(f"SELECT * FROM {model.__tablename__}")))
            cols = list(insp.get_columns(model.__tablename__))
            col_names = [c["name"] for c in cols]
        return [dict(zip(col_names, r)) for r in rows]
    except Exception as e:
        print(f"  [warn] lecture SQLite de {model.__tablename__} impossible ({e}) -> ignore")
        return []


def _cols_of(model):
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(model)
    return {c.key: c for c in mapper.columns}


def migrate_model(model, pg_session):
    rows = _dump_sqlite_rows(model)
    if not rows:
        print(f"  {model.__tablename__}: 0 ligne (ignore)")
        return 0
    cols = _cols_of(model)
    count = 0
    for row in rows:
        # Ne garder que les colonnes existantes dans le modèle PG
        data = {k: v for k, v in row.items() if k in cols}
        obj = model(**data)
        pg_session.add(obj)
        count += 1
    pg_session.flush()
    print(f"  {model.__tablename__}: {count} ligne(s) migrée(s)")
    return count


def main():
    print(f"[MIGRATION] Cible PostgreSQL : {PG_URL}")
    with a.app.app_context():
        # Crée toutes les tables dans PostgreSQL
        db.drop_all()  # sécurité : base vide attendue ; sinon commentez cette ligne
        db.create_all()
        print("[MIGRATION] Schéma PostgreSQL créé.")

        # Ordre respectant les FK
        order = [Site, User, Vehicle, AccessLog, Notification, NotificationRead]

        # Utiliser une session PostgreSQL dédiée
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db.engine)
        pg_session = Session()
        try:
            for model in order:
                migrate_model(model, pg_session)
            pg_session.commit()
            print("[MIGRATION] Données transférées avec succès.")
        except Exception as e:
            pg_session.rollback()
            raise
        finally:
            pg_session.close()

    print("\n[MIGRATION] Terminé.")
    print("Pour utiliser PostgreSQL définitivement, assurez-vous que DATABASE_URL pointe")
    print("vers PostgreSQL au démarrage de l'application (variable d'environnement ou config.yaml).")


if __name__ == "__main__":
    main()
