from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Site(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=False)
    capacity = db.Column(db.Integer, default=50)
    camera_url_entry = db.Column(db.String(255))
    camera_url_exit = db.Column(db.String(255))
    max_hours_student = db.Column(db.Integer, default=8)
    max_hours_visitor = db.Column(db.Integer, default=4)
    access_start = db.Column(db.String(10), default="06:00")
    access_end = db.Column(db.String(10), default="22:00")
    long_stay_hours = db.Column(db.Integer, default=48)
    gate_ip = db.Column(db.String(255))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    full_name = db.Column(db.String(100))
    site = db.Column(db.String(50))
    site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)


class Vehicle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plate_number = db.Column(db.String(20), unique=True, nullable=False)
    owner_name = db.Column(db.String(100), nullable=False)
    owner_phone = db.Column(db.String(30))
    owner_email = db.Column(db.String(120))
    owner_address = db.Column(db.String(255))
    vehicle_model = db.Column(db.String(120))
    vehicle_type = db.Column(db.String(30), default="auto")  # auto, moto, sans_plaque
    function = db.Column(db.String(100))
    site_authorized = db.Column(db.String(80))
    site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True)
    status = db.Column(db.String(20), default='pending')
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AccessLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plate_number = db.Column(db.String(20), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicle.id'), nullable=True)
    action = db.Column(db.String(10))
    status = db.Column(db.String(20))
    site = db.Column(db.String(50))
    site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    guardian_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    duration_minutes = db.Column(db.Integer, default=None)


class Notification(db.Model):
    """Alertes systeme visibles par admin/gardien (lecture par utilisateur)."""
    id = db.Column(db.Integer, primary_key=True)
    site = db.Column(db.String(50))
    site_id = db.Column(db.Integer, db.ForeignKey('site.id'), nullable=True)
    category = db.Column(db.String(30))
    message = db.Column(db.String(500), nullable=False)
    plate_number = db.Column(db.String(20))
    guardian_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    contact_phone = db.Column(db.String(30))
    whatsapp_message = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False, nullable=False)


class NotificationRead(db.Model):
    """Chaque utilisateur marque ses propres notifications comme lues."""
    __table_args__ = (db.UniqueConstraint('notification_id', 'user_id', name='uq_notif_user_read'),)
    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(db.Integer, db.ForeignKey('notification.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    read_at = db.Column(db.DateTime, default=datetime.utcnow)
