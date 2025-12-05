from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin' or 'hospital'

class Donor(db.Model):
    __tablename__ = "donor"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    blood_group = db.Column(db.String(5), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    last_donation_date = db.Column(db.Date, nullable=True)
    is_available = db.Column(db.Boolean, default=True)
    last_seen = db.Column(db.DateTime, nullable=True)
    is_online = db.Column(db.Boolean, default=False)

class BloodRequest(db.Model):
    __tablename__ = "blood_request"
    id = db.Column(db.Integer, primary_key=True)
    patient_name = db.Column(db.String(80), nullable=False)
    required_blood_group = db.Column(db.String(5), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    status = db.Column(db.String(20), default="OPEN")  # OPEN, MATCHED, CLOSED
