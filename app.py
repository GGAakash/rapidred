# app.py - RapidRed (updated)
import os
import json
from datetime import datetime, date, timedelta
from math import radians, sin, cos, sqrt, asin
from functools import wraps
# add this near other imports at top of file
from sqlalchemy import text
import logging

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from flask_socketio import SocketIO, emit, join_room, leave_room

# Optional Twilio
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

# Optional email
import smtplib
from email.mime.text import MIMEText

#####################
# Configuration
#####################
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(DB_DIR, exist_ok=True)
DB_FILE = os.path.join(DB_DIR, "rapidred.db")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL") or f"sqlite:///{DB_FILE}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Uploads
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif"}

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
db = SQLAlchemy(app)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


#####################
# Models
#####################
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(32), default="hospital")  # 'admin','hospital'

    def set_password(self, pwd):
        self.password_hash = generate_password_hash(pwd)

    def check_password(self, pwd):
        return check_password_hash(self.password_hash, pwd)


class Donor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    blood_group = db.Column(db.String(10), nullable=False)
    phone = db.Column(db.String(32), unique=True, nullable=False)  # phone used as username
    password_hash = db.Column(db.String(256), nullable=True)  # donors may set password on register
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    last_donation_date = db.Column(db.Date, nullable=True)
    is_available = db.Column(db.Boolean, default=True)
    is_online = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime, nullable=True)

    # biodata
    dob = db.Column(db.Date, nullable=True)
    age = db.Column(db.Integer, nullable=True)
    weight_kg = db.Column(db.Float, nullable=True)
    chronic_conditions = db.Column(db.String(512), nullable=True)
    health_clearance = db.Column(db.Boolean, default=False)
    consent = db.Column(db.Boolean, default=True)
    photo = db.Column(db.String(256), nullable=True)


class BloodRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_name = db.Column(db.String(120), nullable=False)
    required_blood_group = db.Column(db.String(10), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(32), default="OPEN")  # OPEN, ACCEPTED, CANCELLED, FULFILLED
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    accepted_donor_id = db.Column(db.Integer, db.ForeignKey("donor.id"), nullable=True)


#####################
# Utilities
#####################
MIN_AGE = 18
MAX_AGE = 65
MIN_WEIGHT_KG = 50.0
MIN_DAYS_SINCE_LAST_DONATION = 90


def haversine(lat1, lon1, lat2, lon2):
    try:
        if None in (lat1, lon1, lat2, lon2):
            return float("inf")
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * asin(sqrt(a))
        return 6371 * c
    except Exception:
        return float("inf")


def compute_age_from_dob(dob):
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please login first.", "error")
                return redirect(url_for("login"))
            if role and session.get("user_role") != role:
                flash("Unauthorized access.", "error")
                return redirect(url_for("index"))
            return f(*args, **kwargs)

        return wrapped

    return decorator


def donor_login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "donor_id" not in session:
            flash("Please login as donor.", "error")
            return redirect(url_for("donor_login"))
        return f(*args, **kwargs)

    return wrapped


#####################
# Comm helpers (SMS / Email)
#####################
def send_sms_twilio(phone, message):
    sid = os.getenv("TWILIO_SID")
    token = os.getenv("TWILIO_TOKEN")
    from_num = os.getenv("TWILIO_PHONE")
    if not (sid and token and from_num and TwilioClient):
        # mock for local testing
        print("[SMS MOCK] ->", phone, message)
        return False
    try:
        client = TwilioClient(sid, token)
        client.messages.create(from_=from_num, to=phone, body=message)
        return True
    except Exception as e:
        print("Twilio error:", e)
        return False


def send_email_smtp(to_email, subject, body):
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    if not (user and pwd):
        print("[EMAIL MOCK] ->", to_email, subject, body)
        return False
    try:
        msg = MIMEText(body)
        msg["From"] = user
        msg["To"] = to_email
        msg["Subject"] = subject
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        s.login(user, pwd)
        s.sendmail(user, [to_email], msg.as_string())
        s.quit()
        return True
    except Exception as e:
        print("SMTP error:", e)
        return False


#####################
# Eligibility
#####################
def is_donor_eligible(d: Donor):
    if not d.is_available:
        return False, "Not available"
    if not d.consent:
        return False, "No consent"
    if d.health_clearance is False:
        return False, "No health clearance"
    if d.weight_kg is None:
        return False, "Weight unknown"
    try:
        if float(d.weight_kg) < MIN_WEIGHT_KG:
            return False, "Underweight"
    except Exception:
        return False, "Invalid weight"
    donor_age = d.age
    if donor_age is None and d.dob:
        donor_age = compute_age_from_dob(d.dob)
    if donor_age is None:
        return False, "Age unknown"
    if donor_age < MIN_AGE:
        return False, "Too young"
    if MAX_AGE and donor_age > MAX_AGE:
        return False, "Too old"
    if d.last_donation_date:
        days = (datetime.utcnow().date() - d.last_donation_date).days
        if days < MIN_DAYS_SINCE_LAST_DONATION:
            return False, f"Donated {days} days ago"
    return True, "Eligible"


#####################
# Routes - Public
#####################
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


#####################
# Donor registration & login
#####################
@app.route("/register_donor", methods=["GET", "POST"])
def register_donor():
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        blood = request.form.get("blood_group", "").strip().upper()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()
        dob_str = request.form.get("dob", "").strip()
        weight = request.form.get("weight_kg", "").strip()
        chronic = request.form.get("chronic_conditions", "").strip()
        consent = request.form.get("consent") == "yes"
        health_clearance = request.form.get("health_clearance") == "yes"

        if not (name and blood and phone and password):
            error = "Please fill name, blood group, phone and choose a password."
            return render_template("register_donor.html", error=error)

        # duplicate phone
        if Donor.query.filter_by(phone=phone).first():
            error = "Phone already registered."
            return render_template("register_donor.html", error=error)

        dob = None
        if dob_str:
            try:
                dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
            except:
                dob = None

        try:
            weight_val = float(weight) if weight else None
        except:
            weight_val = None

        d = Donor(
            name=name,
            blood_group=blood,
            phone=phone,
            latitude=None,
            longitude=None,
            weight_kg=weight_val,
            chronic_conditions=chronic,
            health_clearance=health_clearance,
            consent=consent,
            dob=dob,
            age=compute_age_from_dob(dob) if dob else None,
            is_available=True,
            last_seen=datetime.utcnow(),
        )
        d.password_hash = generate_password_hash(password)
        db.session.add(d)
        db.session.commit()

        # notify admin/hospitals
        sid = os.getenv("ADMIN_NOTIFY_EMAIL")
        if sid:
            send_email_smtp(sid, "New donor registered", f"{d.name} ({d.blood_group}) registered.")

        flash("Registration successful. Please login as donor if you want to share location.", "info")
        return redirect(url_for("donor_login"))
    return render_template("register_donor.html", error=error)


@app.route("/donor/login", methods=["GET", "POST"])
def donor_login():
    error = None
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        pwd = request.form.get("password", "")
        d = Donor.query.filter_by(phone=phone).first()
        if not d or not d.password_hash or not check_password_hash(d.password_hash, pwd):
            error = "Invalid phone or password."
            return render_template("donor_login.html", error=error)
        session["donor_id"] = d.id
        session["donor_name"] = d.name
        flash("Logged in as donor.", "info")
        return redirect(url_for("donor_dashboard"))
    return render_template("donor_login.html", error=error)


@app.route("/donor/logout")
def donor_logout():
    session.pop("donor_id", None)
    session.pop("donor_name", None)
    flash("Logged out.", "info")
    return redirect(url_for("index"))


@app.route("/donor/dashboard")
@donor_login_required
def donor_dashboard():
    d = Donor.query.get(session["donor_id"])
    return render_template("donor_dashboard.html", donor=d)


#####################
# Donor share (JS page) will emit via Socket.IO; we also provide a route for donor to go to share page
#####################
@app.route("/donor/share")
@donor_login_required
def donor_share_page():
    d = Donor.query.get(session["donor_id"])
    return render_template("donor_share.html", donor=d)


#####################
# Hospital/Admin auth
#####################
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pwd = request.form.get("password", "")
        u = User.query.filter_by(username=username).first()
        if not u or not u.check_password(pwd):
            error = "Invalid credentials"
            return render_template("login.html", error=error)
        session["user_id"] = u.id
        session["username"] = u.username
        session["user_role"] = u.role
        flash("Logged in", "info")
        if u.role == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("request_blood"))
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    for k in list(session.keys()):
        session.pop(k, None)
    flash("Logged out.", "info")
    return redirect(url_for("index"))


#####################
# Hospital request flows
#####################
@app.route("/request_blood", methods=["GET", "POST"])
@login_required(role="hospital")
def request_blood():
    error = None
    if request.method == "POST":
        patient = request.form.get("patient_name", "").strip()
        blood = request.form.get("required_blood_group", "").strip().upper()
        lat = request.form.get("latitude")
        lon = request.form.get("longitude")
        top_k = int(request.form.get("top_k", 10))

        try:
            latf = float(lat)
            lonf = float(lon)
        except Exception:
            error = "Invalid coordinates"
            return render_template("request_blood.html", error=error, matches=[], rejected=[], form={})

        br = BloodRequest(
            patient_name=patient,
            required_blood_group=blood,
            latitude=latf,
            longitude=lonf,
            status="OPEN",
            created_by=session.get("user_id"),
        )
        db.session.add(br)
        db.session.commit()

        # match donors (same blood only; later expand to compatibility)
        donors = Donor.query.filter_by(blood_group=blood).all()
        matches = []
        rejected = []
        for d in donors:
            ok, reason = is_donor_eligible(d)
            if not ok:
                rejected.append({"donor": d, "reason": reason})
                continue
            if d.latitude is None or d.longitude is None:
                rejected.append({"donor": d, "reason": "No location"})
                continue
            dist = haversine(latf, lonf, d.latitude, d.longitude)
            score = max(0.0, 1.0 - (dist / 60.0))  # rough
            matches.append({"donor": d, "distance_km": dist, "score": score})

        matches_sorted = sorted(matches, key=lambda x: x["score"], reverse=True)
        top_matches = matches_sorted[:top_k]

        # notify top matches with SMS mock / real
        notified = []
        for m in top_matches:
            d = m["donor"]
            msg = f"URGENT: Blood needed ({blood}) for {patient} near your area. Reply if available."
            sms_ok = False
            if d.phone:
                try:
                    sms_ok = send_sms_twilio(d.phone, msg)
                except Exception:
                    sms_ok = False
            notified.append({"donor_id": d.id, "name": d.name, "phone": d.phone, "distance_km": round(m["distance_km"], 2), "score": round(m["score"], 3), "sms_sent": sms_ok})

        # prepare safe matches for JS (primitive)
        safe_matches = []
        for m in top_matches:
            d = m["donor"]
            safe_matches.append({
                "id": d.id,
                "name": d.name,
                "blood_group": d.blood_group,
                "phone": d.phone,
                "latitude": d.latitude,
                "longitude": d.longitude,
                "distance_km": round(m["distance_km"], 2),
                "score": round(m["score"], 3),
                "is_online": bool(d.is_online)
            })

        # emit request created to hospital room
        try:
            socketio.emit("request_created", {"id": br.id, "patient_name": br.patient_name, "required_blood_group": br.required_blood_group, "latitude": br.latitude, "longitude": br.longitude, "status": br.status}, room="hospitals")
        except:
            pass

        return render_template("results.html", notified=notified, rejected=rejected, matches=safe_matches, request_id=br.id)

    # GET
    return render_template("request_blood.html", error=None, matches=[], rejected=[], form={})


@app.route("/hospital/requests")
@login_required(role="hospital")
def hospital_requests():
    rows = BloodRequest.query.order_by(BloodRequest.created_at.desc()).all()
    out = []
    for r in rows:
        accepted = None
        if r.accepted_donor_id:
            d = Donor.query.get(r.accepted_donor_id)
            accepted = {"id": d.id, "name": d.name, "phone": d.phone} if d else None
        out.append({
            "id": r.id,
            "patient_name": r.patient_name,
            "blood_group": r.required_blood_group,
            "latitude": r.latitude,
            "longitude": r.longitude,
            "created_at": r.created_at,
            "status": r.status,
            "accepted": accepted
        })
    return render_template("hospital_requests.html", requests=out)


@app.route("/cancel_request/<int:request_id>", methods=["POST"])
@login_required(role="hospital")
def cancel_request(request_id):
    br = BloodRequest.query.get_or_404(request_id)
    br.status = "CANCELLED"
    db.session.commit()
    socketio.emit("request_cancelled", {"id": br.id}, room="hospitals")
    flash("Request cancelled.", "info")
    return redirect(url_for("hospital_requests"))


@app.route("/accept_request/<int:request_id>", methods=["POST"])
@donor_login_required
def accept_request(request_id):
    br = BloodRequest.query.get_or_404(request_id)
    if br.status != "OPEN":
        flash("Request not open.", "error")
        return redirect(url_for("donor_dashboard"))
    br.accepted_donor_id = session["donor_id"]
    br.status = "ACCEPTED"
    db.session.commit()
    # notify hospital/admin
    socketio.emit("request_accepted", {"request_id": br.id, "donor_id": br.accepted_donor_id}, room="hospitals")
    flash("Request accepted. Hospital will be notified and you will be tracked.", "info")
    return redirect(url_for("donor_dashboard"))


#####################
# Admin pages
#####################
@app.route("/admin/dashboard")
@login_required(role="admin")
def admin_dashboard():
    total_donors = Donor.query.count()
    total_requests = BloodRequest.query.count()
    open_requests = BloodRequest.query.filter_by(status="OPEN").count()
    active_donors = Donor.query.filter_by(is_online=True).count()
    recent_requests = BloodRequest.query.order_by(BloodRequest.created_at.desc()).limit(8).all()
    return render_template("admin_dashboard.html", total_donors=total_donors, total_requests=total_requests, open_requests=open_requests, active_donors=active_donors, recent_requests=recent_requests)


@app.route("/admin/map")
@login_required(role="admin")
def admin_map():
    return render_template("admin_map.html")


@app.route("/donors")
@login_required(role="admin")
def donors_list():
    q = request.args.get("q", "").strip()
    bg = request.args.get("blood_group", "").strip()
    query = Donor.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Donor.name.ilike(like), Donor.phone.ilike(like)))
    if bg:
        query = query.filter_by(blood_group=bg)
    donors = query.order_by(Donor.id.desc()).all()
    return render_template("donors.html", donors=donors)


@app.route("/edit_donor/<int:donor_id>", methods=["GET", "POST"])
@login_required(role="admin")
def edit_donor(donor_id):
    d = Donor.query.get_or_404(donor_id)
    if request.method == "POST":
        d.name = request.form.get("name", d.name)
        d.blood_group = request.form.get("blood_group", d.blood_group)
        d.phone = request.form.get("phone", d.phone)
        d.is_available = request.form.get("is_available") == "yes"
        try:
            w = request.form.get("weight_kg")
            d.weight_kg = float(w) if w else d.weight_kg
        except:
            pass
        db.session.commit()
        flash("Donor updated", "info")
        return redirect(url_for("donors_list"))
    return render_template("edit_donor.html", donor=d)


@app.route("/delete_donor/<int:donor_id>", methods=["POST"])
@login_required(role="admin")
def delete_donor(donor_id):
    d = Donor.query.get(donor_id)
    if not d:
        flash("Not found", "error")
        return redirect(url_for("donors_list"))
    db.session.delete(d)
    db.session.commit()
    flash("Donor deleted", "info")
    return redirect(url_for("donors_list"))


#####################
# APIs
#####################
@app.route("/api/all_donors")
@login_required(role="admin")
def api_all_donors():
    donors = Donor.query.all()
    out = []
    for d in donors:
        out.append({"id": d.id, "name": d.name, "phone": d.phone, "blood_group": d.blood_group, "latitude": d.latitude, "longitude": d.longitude, "is_online": d.is_online, "last_seen": d.last_seen.isoformat() if d.last_seen else None})
    return jsonify(out)


#####################
# SocketIO events
#####################
@socketio.on("donor_share")
def handle_donor_share(data):
    try:
        donor_id = int(data.get("donor_id"))
        lat = float(data.get("latitude"))
        lon = float(data.get("longitude"))
    except Exception as e:
        print("donor_share invalid", e)
        return
    d = Donor.query.get(donor_id)
    if not d:
        return
    d.latitude = lat
    d.longitude = lon
    d.is_online = True
    d.last_seen = datetime.utcnow()
    db.session.commit()
    payload = {"id": d.id, "name": d.name, "phone": d.phone, "blood_group": d.blood_group, "latitude": d.latitude, "longitude": d.longitude, "is_online": d.is_online, "last_seen": d.last_seen.isoformat() if d.last_seen else None}
    emit("donor_updated", payload, broadcast=True)


@socketio.on("donor_offline")
def handle_donor_offline(data):
    try:
        donor_id = int(data.get("donor_id"))
    except:
        return
    d = Donor.query.get(donor_id)
    if d:
        d.is_online = False
        d.last_seen = datetime.utcnow()
        db.session.commit()
        emit("donor_offline", {"id": d.id}, broadcast=True)


@socketio.on("join_room")
def on_join(data):
    room = data.get("room")
    if room:
        join_room(room)
        emit("joined_room", {"room": room}, room=request.sid)


@socketio.on("leave_room")
def on_leave(data):
    room = data.get("room")
    if room:
        leave_room(room)
        emit("left_room", {"room": room}, room=request.sid)


#####################
# Init / DB reset helper
#####################
def ensure_admin():
    admin_user = os.getenv('ADMIN_USER', 'admin')
    admin_pass = os.getenv('ADMIN_PASS', 'admin123')
    u = User.query.filter_by(username=admin_user).first()
    if not u:
        u = User(username=admin_user, role='admin')
        u.set_password(admin_pass)
        db.session.add(u)
        db.session.commit()
        print("[INIT] Created admin user:", admin_user)

def init_db(reset: bool = False):
    """
    Initialize DB and attempt best-effort schema migration.
    If reset=True, existing DB file is removed (dev convenience).
    """
    # Ensure filesystem exists
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)

    if reset and os.path.exists(DB_FILE):
        try:
            os.remove(DB_FILE)
            print("[INIT] Removed existing DB file for reset.")
        except Exception as e:
            print("[INIT] Failed to remove DB file:", e)

    # All DB operations must be inside application context
    with app.app_context():
        # Create base tables from models
        db.create_all()
        print("[INIT] db.create_all() completed")

        # Best-effort migration: add columns if missing
        try:
            # PRAGMA table_info needs to be executed as text()
            info = db.session.execute(text("PRAGMA table_info(donor);")).fetchall()
            cols = [r[1] for r in info] if info else []
            needed_donor = {
                'dob': "DATE",
                'age': "INTEGER",
                'weight_kg': "REAL",
                'chronic_conditions': "VARCHAR",
                'health_clearance': "INTEGER",
                'consent': "INTEGER",
                'photo': "VARCHAR"
            }
            for col, coltype in needed_donor.items():
                if col not in cols:
                    try:
                        db.session.execute(text(f"ALTER TABLE donor ADD COLUMN {col} {coltype};"))
                        print(f"[MIGRATE] Added donor.{col}")
                    except Exception as e:
                        print("[MIGRATE] could not add donor.", col, e)
            # blood_request table migrations
            info_req = db.session.execute(text("PRAGMA table_info(blood_request);")).fetchall()
            cols_req = [r[1] for r in info_req] if info_req else []
            needed_req = {
                'accepted_donor_id': "INTEGER",
                'assigned_at': "DATETIME"
            }
            for col, coltype in needed_req.items():
                if col not in cols_req:
                    try:
                        db.session.execute(text(f"ALTER TABLE blood_request ADD COLUMN {col} {coltype};"))
                        print(f"[MIGRATE] Added blood_request.{col}")
                    except Exception as e:
                        print("[MIGRATE] could not add blood_request.", col, e)

            db.session.commit()
        except Exception as e:
            # If PRAGMA or ALTER fails, don't crash — log and continue
            print("[MIGRATE] PRAGMA/ALTER step failed:", e)

        # create admin user if missing
        try:
            ensure_admin()
        except Exception as e:
            print("[INIT] ensure_admin failed:", e)

        print("[INIT] DB ready")

if __name__ == '__main__':
    # read RESET_DB environment variable (common in CI) to optionally wipe DB
    reset_flag = False
    init_db(reset=reset_flag)

    print("[INFO] Starting RapidRed app...")
    # Use socketio.run to support Socket.IO
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)