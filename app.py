# ============================
# EVENTLET MONKEY PATCH (MUST BE FIRST)
# ============================
import eventlet
eventlet.monkey_patch()

# ============================
# NORMAL IMPORTS
# ============================
import os
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from flask_socketio import SocketIO, emit, join_room, leave_room

from models import db, User, Donor, BloodRequest, Notification
from matching import find_best_donors, canonical_blood, haversine_distance

# Twilio optional
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

import smtplib
from email.mime.text import MIMEText

# ============================
# PATHS & CONFIG
# ============================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOADS = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")

# ----- PostgreSQL or SQLite auto configure -----
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'rapidred.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOADS

db.init_app(app)

# SocketIO with eventlet
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


# ============================
# HELPERS
# ============================
def compute_age_from_dob(dob):
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if "user_role" not in session:
                flash("Login required.", "error")
                return redirect(url_for("login"))
            if role and session["user_role"] != role:
                flash("Unauthorized.", "error")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def donor_login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get("user_role") != "donor":
            flash("Please login as donor.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


def send_email_smtp(to_email, subject, body):
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")

    if not user or not pwd:
        print("EMAIL MOCK:", subject, body)
        return False

    msg = MIMEText(body)
    msg["From"] = user
    msg["To"] = to_email
    msg["Subject"] = subject

    try:
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        s.login(user, pwd)
        s.sendmail(user, [to_email], msg.as_string())
        s.quit()
        return True
    except:
        return False


# ============================
# ROUTES
# ============================

@app.route("/")
def index():
    role = session.get("user_role")
    notifications = []

    if role == "donor":
        did = session.get("donor_id")
        notifications = Notification.query.filter_by(donor_id=did).order_by(Notification.created_at.desc()).all()
    elif role in ("hospital", "admin"):
        notifications = Notification.query.order_by(Notification.created_at.desc()).limit(20).all()

    return render_template("index.html", notifications=notifications)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form["identifier"].strip()
        pwd = request.form["password"]

        # Admin/Hospital User login
        u = User.query.filter_by(username=identifier).first()
        if u and check_password_hash(u.password_hash, pwd):
            session.clear()
            session["user_id"] = u.id
            session["username"] = u.username
            session["user_role"] = u.role
            flash("Login successful.", "success")

            if u.role == "admin":
                return redirect(url_for("admin_dashboard"))
            if u.role == "hospital":
                return redirect(url_for("request_blood"))

        # Donor login
        d = Donor.query.filter_by(phone=identifier).first()
        if d and check_password_hash(d.password_hash, pwd):
            session.clear()
            session["donor_id"] = d.id
            session["user_role"] = "donor"
            session["donor_name"] = d.name
            flash("Logged in as donor.", "success")
            return redirect(url_for("donor_dashboard"))

        flash("Invalid login.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ==========================================
# DONOR REGISTRATION
# ==========================================
@app.route("/register_donor", methods=["GET", "POST"])
def register_donor():
    if request.method == "POST":
        name = request.form["name"]
        phone = request.form["phone"]
        blood = request.form["blood_group"].upper()
        password = request.form["password"]

        d = Donor(
            name=name,
            phone=phone,
            blood_group=blood,
            is_available=True,
            last_seen=datetime.utcnow()
        )
        d.password_hash = generate_password_hash(password)

        db.session.add(d)
        db.session.commit()

        flash("Registration successful!", "success")
        return redirect(url_for("login"))

    return render_template("register_donor.html")


# ==========================================
# DONOR DASHBOARD
# ==========================================
@app.route("/donor/dashboard")
@donor_login_required
def donor_dashboard():
    d = Donor.query.get(session["donor_id"])
    assigned = BloodRequest.query.filter_by(accepted_donor_id=d.id).all()
    notifications = Notification.query.filter_by(donor_id=d.id).all()
    return render_template("donor_dashboard.html", donor=d, assigned_requests=assigned, notifications=notifications)


# ==========================================
# HOSPITAL — CREATE BLOOD REQUEST
# ==========================================
@app.route("/request_blood", methods=["GET", "POST"])
@login_required(role="hospital")
def request_blood():
    if request.method == "POST":
        patient = request.form["patient_name"]
        blood = request.form["required_blood_group"].upper()
        lat = float(request.form["latitude"])
        lon = float(request.form["longitude"])

        br = BloodRequest(
            patient_name=patient,
            required_blood_group=blood,
            latitude=lat,
            longitude=lon,
            status="OPEN",
            created_at=datetime.utcnow(),
            created_by=session["user_id"]
        )
        db.session.add(br)
        db.session.commit()

        # find donors
        ranked = find_best_donors(blood, lat, lon, max_results=10)
        for d, dist, sc in ranked:
            notif = Notification(donor_id=d.id, request_id=br.id, notif_type="REQUEST",
                                 payload="URGENT BLOOD REQUEST NEAR YOU!")
            db.session.add(notif)
        db.session.commit()

        return render_template("results.html", matches=ranked, request_id=br.id)

    return render_template("request_blood.html")


# ==========================================
# HOSPITAL REQUEST PAGE
# ==========================================
@app.route("/hospital/requests")
@login_required(role="hospital")
def hospital_requests():
    rows = BloodRequest.query.order_by(BloodRequest.created_at.desc()).all()

    out = []
    for r in rows:
        acc = None
        if r.accepted_donor_id:
            d = Donor.query.get(r.accepted_donor_id)
            acc = {"id": d.id, "name": d.name, "phone": d.phone}

        out.append({
            "id": r.id,
            "patient_name": r.patient_name,
            "blood_group": r.required_blood_group,
            "status": r.status,
            "accepted": acc,
            "latitude": r.latitude,
            "longitude": r.longitude
        })

    return render_template("hospital_requests.html", requests=out)


# ==========================================
# HOSPITAL ACTIONS (REACHED / COMPLETED)
# ==========================================
@app.route("/hospital/mark_reached/<int:request_id>", methods=["POST"])
@login_required(role="hospital")
def hospital_mark_reached(request_id):
    br = BloodRequest.query.get_or_404(request_id)
    br.status = "DONOR_REACHED"
    db.session.commit()

    socketio.emit("request_updated", {"id": br.id, "status": br.status}, broadcast=True)

    flash("Marked as donor reached.", "success")
    return redirect(url_for("hospital_requests"))


@app.route("/hospital/mark_completed/<int:request_id>", methods=["POST"])
@login_required(role="hospital")
def hospital_mark_completed(request_id):
    br = BloodRequest.query.get_or_404(request_id)
    br.status = "FULFILLED"
    db.session.commit()

    socketio.emit("request_updated", {"id": br.id, "status": br.status}, broadcast=True)

    flash("Request marked completed.", "success")
    return redirect(url_for("hospital_requests"))


# ==========================================
# ADMIN DASHBOARD
# ==========================================
@app.route("/admin/dashboard")
@login_required(role="admin")
def admin_dashboard():
    donors = Donor.query.count()
    total = BloodRequest.query.count()
    open_r = BloodRequest.query.filter_by(status="OPEN").count()

    recent = BloodRequest.query.order_by(BloodRequest.created_at.desc()).limit(8).all()

    return render_template("admin_dashboard.html",
                           total_donors=donors,
                           total_requests=total,
                           open_requests=open_r,
                           recent_requests=recent)


# ==========================================
# SOCKET.IO
# ==========================================
@socketio.on("join")
def on_join(data):
    rid = data.get("donor_id")
    if rid:
        join_room(f"donor_{rid}")


# ==========================================
# RUN (Render fix — create tables in Postgres)
# ==========================================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()   # 🔥 FIXED: creates tables on Render PostgreSQL

    print("🔥 RapidRed Server starting...")
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
