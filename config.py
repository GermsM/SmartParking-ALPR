import os
import yaml
import pytesseract
from flask import current_app

# Chemin absolu vers le fichier de configuration YAML
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.yaml')

# Valeurs de secours par défaut en cas d'absence du fichier YAML
yaml_config = {
    "tesseract_cmd": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    "custom_config": r"--oem 3 --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "database_url": "sqlite:///parking.db",
    "admin_recovery_code": "UCB-RECOVERY-2026",
    "secret_key": "ucb_parking_secret_key_2026",
    "smtp": {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "system.parking.ucbukavu@gmail.com",
        "password": "ksrouzqyhllsytjl",
        "from_address": "system.parking.ucbukavu@gmail.com"
    },
    "forbidden_yolo_classes": ["truck", "bus"],
    "forbidden_vehicle_types": ["camping_car", "poids_lourd", "bus"],
}

# Chargement du fichier YAML s'il existe
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            loaded_data = yaml.safe_load(f)
            if loaded_data:
                yaml_config.update(loaded_data)
    except Exception as e:
        print("Erreur lors de la lecture de config.yaml:", str(e))

# Configuration de Tesseract OCR
pytesseract.pytesseract.tesseract_cmd = yaml_config.get("tesseract_cmd")
custom_config = yaml_config.get("custom_config")

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

print("Configuration Tesseract chargee avec succes")
print("Chemin utilise :", pytesseract.pytesseract.tesseract_cmd)

# Configuration de la base de données
SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', yaml_config.get("database_url"))
if SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace("postgres://", "postgresql://", 1)
SQLALCHEMY_TRACK_MODIFICATIONS = False
SECRET_KEY = os.environ.get('UCB_SECRET_KEY', yaml_config.get("secret_key"))

# Code de récupération de l'administrateur
ADMIN_RECOVERY_CODE = os.environ.get('UCB_ADMIN_RECOVERY', yaml_config.get("admin_recovery_code"))

# Configuration SMTP
smtp_settings = yaml_config.get("smtp", {})
SMTP_HOST = smtp_settings.get("host", "smtp.gmail.com")
SMTP_PORT = int(smtp_settings.get("port", 587))
SMTP_USER = smtp_settings.get("user", "")
SMTP_PASSWORD = smtp_settings.get("password", "")
SMTP_FROM = smtp_settings.get("from_address", "")

# Classes et types interdits
FORBIDDEN_YOLO_CLASSES = frozenset(yaml_config.get("forbidden_yolo_classes", ["truck", "bus"]))
FORBIDDEN_VEHICLE_TYPES = frozenset(yaml_config.get("forbidden_vehicle_types", ["camping_car", "poids_lourd", "bus"]))

# Capacité de secours globale
PARKING_CAPACITY = 120

# Propriétés dynamiques pour la compatibilité avec le reste du projet
# Ces propriétés résolvent les valeurs depuis la base de données s'il y a un contexte d'application.
def get_ucb_sites():
    try:
        if current_app:
            with current_app.app_context():
                from models import Site
                return [s.name for s in Site.query.order_by(Site.name).all()]
    except Exception:
        pass
    return []


def get_site_config():
    config_dict = {}
    try:
        if current_app:
            with current_app.app_context():
                from models import Site
                for s in Site.query.all():
                    config_dict[s.name] = {
                        "capacity": s.capacity,
                        "code": s.code,
                        "camera_url_entry": s.camera_url_entry,
                        "camera_url_exit": s.camera_url_exit,
                        "max_hours_student": s.max_hours_student,
                        "max_hours_visitor": s.max_hours_visitor,
                        "access_start": s.access_start,
                        "access_end": s.access_end,
                        "long_stay_hours": s.long_stay_hours,
                    }
                return config_dict
    except Exception:
        pass
    return {}


def __getattr__(name):
    if name == "UCB_SITES":
        return get_ucb_sites()
    elif name == "SITE_CONFIG":
        return get_site_config()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def get_site_capacity(site_name: str | None) -> int:
    if site_name:
        try:
            if current_app:
                from models import Site
                s = Site.query.filter_by(name=site_name).first()
                if s:
                    return s.capacity
        except Exception:
            pass
    return PARKING_CAPACITY


def get_site_camera_url_entry(site_name: str | None) -> str:
    if site_name:
        try:
            if current_app:
                from models import Site
                s = Site.query.filter_by(name=site_name).first()
                if s:
                    return s.camera_url_entry or ""
        except Exception:
            pass
    return ""


def get_site_camera_url_exit(site_name: str | None) -> str:
    if site_name:
        try:
            if current_app:
                from models import Site
                s = Site.query.filter_by(name=site_name).first()
                if s:
                    return s.camera_url_exit or ""
        except Exception:
            pass
    return ""


# Rétrocompatibilité pour l'ancien getter de caméra unique
def get_site_camera_url(site_name: str | None) -> str:
    return get_site_camera_url_entry(site_name)


def get_site_policy(site_name: str | None) -> dict:
    if site_name:
        try:
            if current_app:
                from models import Site
                s = Site.query.filter_by(name=site_name).first()
                if s:
                    return {
                        "capacity": s.capacity,
                        "code": s.code,
                        "camera_url_entry": s.camera_url_entry,
                        "camera_url_exit": s.camera_url_exit,
                        "max_hours_student": s.max_hours_student,
                        "max_hours_visitor": s.max_hours_visitor,
                        "access_start": s.access_start,
                        "access_end": s.access_end,
                        "long_stay_hours": s.long_stay_hours,
                    }
        except Exception:
            pass
    return {
        "capacity": PARKING_CAPACITY,
        "max_hours_student": 8,
        "max_hours_visitor": 4,
        "access_start": "06:00",
        "access_end": "22:00",
        "long_stay_hours": 48,
    }


def save_yaml_config():
    """Sauvegarde la configuration actuelle dans le fichier YAML."""
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.safe_dump(yaml_config, f, default_flow_style=False, allow_unicode=True)
    except Exception as e:
        print("Erreur lors de l'ecriture dans config.yaml :", str(e))
