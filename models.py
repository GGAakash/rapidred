# models.py
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(32), default="hospital")  # 'admin','hospital'

class Donor(db.Model):
    __tablename__ = "donor"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    blood_group = db.Column(db.String(10), nullable=False)
    phone = db.Column(db.String(32), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    last_donation_date = db.Column(db.Date, nullable=True)
    is_available = db.Column(db.Boolean, default=True)
    is_online = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime, nullable=True)

    dob = db.Column(db.Date, nullable=True)
    age = db.Column(db.Integer, nullable=True)
    weight_kg = db.Column(db.Float, nullable=True)
    chronic_conditions = db.Column(db.String(512), nullable=True)
    health_clearance = db.Column(db.Boolean, default=False)
    consent = db.Column(db.Boolean, default=True)
    photo = db.Column(db.String(256), nullable=True)
    residential_area = db.Column(db.String(256), nullable=True)

class BloodRequest(db.Model):
    __tablename__ = "blood_request"
    id = db.Column(db.Integer, primary_key=True)
    patient_name = db.Column(db.String(120), nullable=False)
    required_blood_group = db.Column(db.String(10), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(32), default="OPEN")  # OPEN, ACCEPTED, DONOR_REACHED, FULFILLED, CANCELLED
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    accepted_donor_id = db.Column(db.Integer, db.ForeignKey("donor.id"), nullable=True)
    assigned_at = db.Column(db.DateTime, nullable=True)

class Notification(db.Model):
    __tablename__ = "notification"
    id = db.Column(db.Integer, primary_key=True)
    donor_id = db.Column(db.Integer, db.ForeignKey("donor.id"), nullable=True)
    request_id = db.Column(db.Integer, db.ForeignKey("blood_request.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notif_type = db.Column(db.String(64), default="REQUEST")
    payload = db.Column(db.String(1024), nullable=True)
    delivered = db.Column(db.Boolean, default=False)
