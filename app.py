# app.py
import os
from datetime import datetime, date
from functools import wraps
try:
    import eventlet
    eventlet.monkey_patch()
except Exception:
    pass
from sqlalchemy import inspect, text

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

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(DB_DIR, exist_ok=True)
DB_FILE = os.path.join(DB_DIR, "rapidred.db")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL") or f"sqlite:///{DB_FILE}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif"}

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

MIN_AGE = 18
MAX_AGE = 65
MIN_WEIGHT_KG = 50.0
MIN_DAYS_SINCE_LAST_DONATION = 90

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
        if session.get("user_role") != "donor" or "donor_id" not in session:
            flash("Please login as donor.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

def send_sms_twilio(phone, message):
    sid = os.getenv("TWILIO_SID")
    token = os.getenv("TWILIO_TOKEN")
    from_num = os.getenv("TWILIO_PHONE")
    if not (sid and token and from_num and TwilioClient):
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

@app.route("/")
def index():
    user_role = session.get("user_role")
    notif_list = []
    if user_role == "donor":
        donor_id = session.get("donor_id")
        if donor_id:
            notif_list = Notification.query.filter_by(donor_id=donor_id).order_by(Notification.created_at.desc()).limit(20).all()
    elif user_role in ("admin", "hospital"):
        notif_list = Notification.query.order_by(Notification.created_at.desc()).limit(20).all()
    return render_template("index.html", notifications=notif_list)

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        pwd = request.form.get("password", "")
        u = User.query.filter_by(username=identifier).first()
        if u and check_password_hash(u.password_hash, pwd):
            session.clear()
            session["user_id"] = u.id
            session["username"] = u.username
            session["user_role"] = u.role
            flash("Logged in", "info")
            if u.role == "admin":
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("request_blood"))
        d = Donor.query.filter_by(phone=identifier).first()
        if d and d.password_hash and check_password_hash(d.password_hash, pwd):
            session.clear()
            session["user_role"] = "donor"
            session["donor_id"] = d.id
            session["donor_name"] = d.name
            flash("Logged in as donor.", "info")
            return redirect(url_for("donor_dashboard"))
        error = "Invalid credentials"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    for k in list(session.keys()):
        session.pop(k, None)
    flash("Logged out.", "info")
    return redirect(url_for("index"))

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
        residential_area = request.form.get("residential_area", "").strip()

        if not (name and blood and phone and password):
            error = "Please fill name, blood group, phone and password."
            return render_template("register_donor.html", error=error)

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
            residential_area=residential_area
        )
        d.password_hash = generate_password_hash(password)
        db.session.add(d)
        db.session.commit()
        flash("Registration successful. Please login.", "info")
        return redirect(url_for("login"))
    return render_template("register_donor.html", error=error)

# hospital registration (self-service)
@app.route("/register_hospital", methods=["GET", "POST"])
def register_hospital():
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()              # optional display name
        username = request.form.get("username", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()
        address = request.form.get("address", "").strip()

        if not (username and password):
            error = "Please provide username and password."
            return render_template("register_hospital.html", error=error, form=request.form)

        # check if username already used
        if User.query.filter_by(username=username).first():
            error = "Username already taken."
            return render_template("register_hospital.html", error=error, form=request.form)

        # create user with role 'hospital'
        try:
            from werkzeug.security import generate_password_hash
            u = User(
                username=username,
                role="hospital",
                password_hash=generate_password_hash(password)
            )

            # optional fields if your User model supports them (avoid if not)
            # If your User model has extra columns, you can set them; otherwise skip.
            # Example: u.display_name = name
            # Example: u.phone = phone
            # Example: u.address = address

            db.session.add(u)
            db.session.commit()
            flash("Hospital account created. Please login.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            db.session.rollback()
            error = f"Failed to create hospital: {str(e)}"
            return render_template("register_hospital.html", error=error, form=request.form)

    # GET
    return render_template("register_hospital.html", error=None, form={})


@app.route("/donor/dashboard")
@donor_login_required
def donor_dashboard():
    d = Donor.query.get(session["donor_id"])
    notifications = Notification.query.filter_by(donor_id=d.id).order_by(Notification.created_at.desc()).limit(20).all()
    assigned = BloodRequest.query.filter_by(accepted_donor_id=d.id).order_by(BloodRequest.created_at.desc()).all()

    # NEW: gather nearby / open requests that this donor has a notification for (or that are open & within general radius)
    available_requests = []
    try:
        # look up notifications of type REQUEST or NEARBY for this donor and fetch request details
        notif_rows = Notification.query.filter(Notification.donor_id == d.id, Notification.notif_type.in_(["REQUEST","NEARBY"])).order_by(Notification.created_at.desc()).all()
        seen_req_ids = set()
        for n in notif_rows:
            if not n.request_id:
                continue
            # avoid duplicates
            if n.request_id in seen_req_ids:
                continue
            br = BloodRequest.query.get(n.request_id)
            if not br:
                continue
            # only show requests that are OPEN (or maybe ACCEPTED but not assigned to someone else)
            if br.status == "OPEN":
                available_requests.append({
                    "id": br.id,
                    "patient_name": br.patient_name,
                    "blood_group": br.required_blood_group,
                    "latitude": br.latitude,
                    "longitude": br.longitude,
                    "created_at": br.created_at,
                    "distance_msg": n.payload or ""
                })
                seen_req_ids.add(br.id)
    except Exception as e:
        # don't crash UI on any error — log and continue
        print("donor_dashboard: nearby list error:", e)

    return render_template("donor_dashboard.html",
                           donor=d,
                           notifications=notifications,
                           assigned_requests=assigned,
                           available_requests=available_requests)

@app.route("/donor/logout")
def donor_logout():
    session.pop("donor_id", None)
    session.pop("donor_name", None)
    session.pop("user_role", None)
    flash("Logged out.", "info")
    return redirect(url_for("index"))

@app.route("/request_blood", methods=["GET", "POST"])
@login_required(role="hospital")
def request_blood():
    error = None
    if request.method == "POST":
        patient = request.form.get("patient_name", "").strip()
        blood = request.form.get("required_blood_group", "").strip().upper()
        lat = request.form.get("latitude")
        lon = request.form.get("longitude")
        try:
            latf = float(lat)
            lonf = float(lon)
        except Exception:
            error = "Invalid coordinates"
            return render_template("request_blood.html", error=error, matches=[], rejected=[], form={})

        top_k = int(request.form.get("top_k", 10))
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

        ranked = find_best_donors(br.required_blood_group, br.latitude, br.longitude, max_results=top_k)

        notified = []
        for d, dist, score in ranked:
            existing = Notification.query.filter_by(donor_id=d.id, request_id=br.id).first()
            if existing:
                continue
            msg_payload = f"URGENT: Blood needed ({br.required_blood_group}) for {br.patient_name} near your area."
            notif = Notification(donor_id=d.id, request_id=br.id, notif_type="REQUEST", payload=msg_payload)
            db.session.add(notif)
            db.session.commit()

            sms_ok = False
            if d.phone:
                try:
                    sms_ok = send_sms_twilio(d.phone, msg_payload)
                except:
                    sms_ok = False

            try:
                socketio.emit("request_notification", {"request_id": br.id, "patient_name": br.patient_name, "blood_group": br.required_blood_group, "latitude": br.latitude, "longitude": br.longitude, "message": msg_payload}, room=f"donor_{d.id}")
            except:
                pass

            notified.append({"donor_id": d.id, "name": d.name, "phone": d.phone, "distance_km": round(dist,2), "score": round(score,3), "sms_sent": sms_ok})

        safe_matches = []
        for d, dist, score in ranked:
            safe_matches.append({
                "id": d.id,
                "name": d.name,
                "blood_group": d.blood_group,
                "phone": d.phone,
                "latitude": d.latitude,
                "longitude": d.longitude,
                "distance_km": round(dist, 2),
                "score": round(score, 3),
                "is_online": bool(d.is_online)
            })

        try:
            socketio.emit("request_created", {"id": br.id, "patient_name": br.patient_name, "required_blood_group": br.required_blood_group, "latitude": br.latitude, "longitude": br.longitude, "status": br.status}, room="hospitals")
        except:
            pass

        return render_template("results.html", notified=notified, matches=safe_matches, request_id=br.id)

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
    br.assigned_at = datetime.utcnow()
    db.session.commit()
    socketio.emit("request_accepted", {"request_id": br.id, "donor_id": br.accepted_donor_id}, room="hospitals")
    flash("Request accepted. Hospital will be notified and you will be tracked.", "info")
    return redirect(url_for("donor_dashboard"))

# cancel assignment & reassign
@app.route("/hospital/cancel_assignment/<int:request_id>/<int:donor_id>", methods=["POST"])
@login_required(role="hospital")
def cancel_assignment(request_id, donor_id):
    br = BloodRequest.query.get_or_404(request_id)
    if br.accepted_donor_id != donor_id:
        flash("This donor is not assigned to the request.", "error")
        return redirect(url_for("hospital_requests"))
    br.accepted_donor_id = None
    br.status = "OPEN"
    br.assigned_at = None
    db.session.commit()
    try:
        socketio.emit("assignment_cancelled", {"request_id": br.id, "message": "Hospital cancelled your assignment."}, room=f"donor_{donor_id}")
    except:
        pass

    top_k = 10
    ranked = find_best_donors(br.required_blood_group, br.latitude, br.longitude, max_results=top_k)
    notified = []
    for d, dist, score in ranked:
        if d.id == donor_id:
            continue
        existing = Notification.query.filter_by(donor_id=d.id, request_id=br.id).first()
        if existing:
            continue
        payload = f"URGENT: Blood needed ({br.required_blood_group}) for {br.patient_name} — reopened."
        notif = Notification(donor_id=d.id, request_id=br.id, notif_type="REQUEST", payload=payload)
        db.session.add(notif)
        db.session.commit()
        try:
            socketio.emit("request_notification", {"request_id": br.id, "patient_name": br.patient_name, "blood_group": br.required_blood_group, "latitude": br.latitude, "longitude": br.longitude, "message": payload}, room=f"donor_{d.id}")
        except:
            pass
        notified.append({"donor_id": d.id, "name": d.name})
    flash(f"Re-notified {len(notified)} donors.", "info")
    return redirect(url_for("hospital_requests"))

# -------------------------
# Hospital marks donor reached (server-side)
# -------------------------
@app.route("/hospital/mark_reached/<int:request_id>", methods=["POST"])
@login_required(role="hospital")
def hospital_mark_reached(request_id):
    br = BloodRequest.query.get_or_404(request_id)
    if not br.accepted_donor_id:
        flash("No donor assigned!", "error")
        return redirect(url_for("hospital_requests"))

    br.status = "DONOR_REACHED"
    db.session.commit()

    # Notify donor room and hospitals/admins that status changed
    try:
        socketio.emit("request_updated", {"id": br.id, "status": br.status, "accepted_donor_id": br.accepted_donor_id}, room="hospitals")
        # notify specific donor room if assigned
        socketio.emit("request_status_change", {"id": br.id, "status": br.status}, room=f"donor_{br.accepted_donor_id}")
    except Exception:
        pass

    flash("Marked donor as reached.", "info")
    return redirect(url_for("hospital_requests"))

# -------------------------
# Hospital marks request completed (server-side)
# -------------------------
@app.route("/hospital/mark_completed/<int:request_id>", methods=["POST"])
@login_required(role="hospital")
def hospital_mark_completed(request_id):
    br = BloodRequest.query.get_or_404(request_id)
    if not br.accepted_donor_id:
        flash("No donor assigned!", "error")
        return redirect(url_for("hospital_requests"))

    br.status = "FULFILLED"
    db.session.commit()

    try:
        socketio.emit("request_updated", {"id": br.id, "status": br.status, "accepted_donor_id": br.accepted_donor_id}, room="hospitals")
        socketio.emit("request_status_change", {"id": br.id, "status": br.status}, room=f"donor_{br.accepted_donor_id}")
    except Exception:
        pass

    flash("Request marked as completed.", "success")
    return redirect(url_for("hospital_requests"))

@app.route("/hospital/reassign/<int:request_id>", methods=["POST"])
@login_required(role="hospital")
def hospital_reassign(request_id):
    br = BloodRequest.query.get_or_404(request_id)
    if br.status in ("CANCELLED", "FULFILLED"):
        flash("Cannot reassign for a closed request.", "error")
        return redirect(url_for("hospital_requests"))
    top_k = int(request.form.get("top_k", 10))
    ranked = find_best_donors(br.required_blood_group, br.latitude, br.longitude, max_results=top_k)
    notified = []
    for d, dist, score in ranked:
        existing = Notification.query.filter_by(donor_id=d.id, request_id=br.id).first()
        if existing:
            continue
        payload = f"URGENT: Blood needed ({br.required_blood_group}) for {br.patient_name} — hospital manual reassign."
        notif = Notification(donor_id=d.id, request_id=br.id, notif_type="REQUEST", payload=payload)
        db.session.add(notif)
        db.session.commit()
        try:
            socketio.emit("request_notification", {"request_id": br.id, "patient_name": br.patient_name, "blood_group": br.required_blood_group, "latitude": br.latitude, "longitude": br.longitude, "message": payload}, room=f"donor_{d.id}")
        except:
            pass
        notified.append({"donor_id": d.id, "name": d.name})
    flash(f"Notified {len(notified)} donors.", "info")
    return redirect(url_for("hospital_requests"))

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


@app.route("/api/all_donors")
@login_required(role="admin")
def api_all_donors():
    donors = Donor.query.all()
    out = []
    for d in donors:
        out.append({"id": d.id, "name": d.name, "phone": d.phone, "blood_group": d.blood_group, "latitude": d.latitude, "longitude": d.longitude, "is_online": d.is_online, "last_seen": d.last_seen.isoformat() if d.last_seen else None})
    return jsonify(out)

@socketio.on("join")
def on_join(data):
    if data.get("room"):
        join_room(data.get("room"))
        emit("joined", {"room": data.get("room")}, room=request.sid)
    if data.get("donor_id"):
        donor_room = f"donor_{data.get('donor_id')}"
        join_room(donor_room)
        emit("joined", {"room": donor_room}, room=request.sid)

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

    open_requests = BloodRequest.query.filter_by(status="OPEN").all()
    for br in open_requests:
        donor_can = canonical_blood(d.blood_group)
        if donor_can is None:
            continue
        try:
            dist = haversine_distance(br.latitude, br.longitude, d.latitude, d.longitude)
        except Exception:
            dist = float('inf')
        if dist <= 10:
            existing = Notification.query.filter_by(donor_id=d.id, request_id=br.id).first()
            if not existing:
                payload = f"Nearby request {br.id} ({br.required_blood_group}) for {br.patient_name}, {round(dist,2)}km"
                notif = Notification(donor_id=d.id, request_id=br.id, notif_type="NEARBY", payload=payload)
                db.session.add(notif)
                db.session.commit()
                try:
                    socketio.emit("nearby_request", {"request_id": br.id, "patient_name": br.patient_name, "blood_group": br.required_blood_group, "distance_km": round(dist,2), "message": payload}, room=f"donor_{d.id}")
                except:
                    pass

    payload = {"id": d.id, "name": d.name, "blood_group": d.blood_group, "latitude": d.latitude, "longitude": d.longitude, "is_online": d.is_online, "last_seen": d.last_seen.isoformat() if d.last_seen else None}
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

@socketio.on("leave")
def on_leave(data):
    room = data.get("room")
    if room:
        leave_room(room)
        emit("left", {"room": room}, room=request.sid)

def ensure_admin():
    admin_user = os.getenv('ADMIN_USER', 'admin')
    admin_pass = os.getenv('ADMIN_PASS', 'admin123')
    u = User.query.filter_by(username=admin_user).first()
    if not u:
        from werkzeug.security import generate_password_hash
        u = User(username=admin_user, role='admin', password_hash=generate_password_hash(admin_pass))
        db.session.add(u)
        db.session.commit()
        print("[INIT] Created admin user:", admin_user)

def init_db(reset: bool = False):
    with app.app_context():
        # create tables if they don't exist
        db.create_all()

        insp = inspect(db.engine)

        # ---- Donor table migrations (add missing columns if necessary) ----
        if insp.has_table("donor"):
            cols = [c["name"] for c in insp.get_columns("donor")]
            required = {
                "dob": "DATE",
                "age": "INTEGER",
                "weight_kg": "FLOAT",
                "chronic_conditions": "TEXT",
                "health_clearance": "BOOLEAN",
                "consent": "BOOLEAN",
                "photo": "TEXT",
                "residential_area": "TEXT"
            }
            for col, coltype in required.items():
                if col not in cols:
                    try:
                        db.session.execute(text(f'ALTER TABLE donor ADD COLUMN "{col}" {coltype}'))
                        print("[MIGRATE] Added donor column:", col)
                    except Exception as e:
                        print("[MIGRATE] donor column skip:", col, e)

        # ---- BloodRequest table migrations ----
        if insp.has_table("blood_request"):
            cols = [c["name"] for c in insp.get_columns("blood_request")]
            required = {
                "accepted_donor_id": "INTEGER",
                "assigned_at": "TIMESTAMP"
            }
            for col, coltype in required.items():
                if col not in cols:
                    try:
                        db.session.execute(text(f'ALTER TABLE blood_request ADD COLUMN "{col}" {coltype}'))
                        print("[MIGRATE] Added request column:", col)
                    except Exception as e:
                        print("[MIGRATE] request column skip:", col, e)

        db.session.commit()

        # ensure admin user exists
        try:
            ensure_admin()
        except Exception as e:
            print("[INIT] ensure_admin failed:", e)

        print("[INIT] Database ready.")

# -----------------------------------------------
# ONE-TIME INITIALIZATION ROUTE FOR RENDER ONLY
# -----------------------------------------------
from werkzeug.security import generate_password_hash

@app.route("/init_db_magic_secret", methods=["GET"])
def init_db_magic():
    secret = request.args.get("s")
    if secret != os.getenv("INIT_SECRET", "macha123"):
        return "Access Denied", 403
    
    try:
        with app.app_context():
            db.create_all()

            # ensure admin user exists
            admin_user = os.getenv("ADMIN_USER", "admin")
            admin_pass = os.getenv("ADMIN_PASS", "admin123")

            if not User.query.filter_by(username=admin_user).first():
                u = User(
                    username=admin_user,
                    role="admin",
                    password_hash=generate_password_hash(admin_pass)
                )
                db.session.add(u)
                db.session.commit()

        return "DB CREATED SUCCESSFULLY, ADMIN READY", 200

    except Exception as e:
        return f"ERROR: {str(e)}", 500
    


if __name__ == '__main__':
    reset_flag = False
    init_db(reset=reset_flag)
    print("[INFO] Starting RapidRed app...")
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
