import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from flask_socketio import SocketIO, emit
from models import db, User, Donor, BloodRequest
from matching import find_best_donors, haversine_distance

# Twilio and SMTP (real usage via env vars)
from twilio.rest import Client as TwilioClient
import smtplib
from email.mime.text import MIMEText

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///rapidred.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change_this_secret_for_dev')

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Helper decorator
def login_required(role=None):
    def wrapper(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if role and session.get('user_role') != role:
                return "Unauthorized", 403
            return fn(*args, **kwargs)
        return decorated
    return wrapper

# Notification helpers
def send_sms_mock(phone, message):
    print(f"[SMS MOCK] to {phone}: {message}")

def send_email_mock(to, subject, body):
    print(f"[EMAIL MOCK] to {to}: {subject}\n{body}")

def send_sms_twilio(phone, message):
    sid = os.getenv('TWILIO_SID')
    token = os.getenv('TWILIO_TOKEN')
    tw_num = os.getenv('TWILIO_PHONE')
    if not (sid and token and tw_num):
        send_sms_mock(phone, message)
        return
    client = TwilioClient(sid, token)
    client.messages.create(from_=tw_num, to=phone, body=message)

def send_email_smtp(to_email, subject, body):
    user = os.getenv('SMTP_USER')
    pwd = os.getenv('SMTP_PASS')
    if not (user and pwd):
        send_email_mock(to_email, subject, body)
        return
    msg = MIMEText(body)
    msg['From'] = user
    msg['To'] = to_email
    msg['Subject'] = subject
    s = smtplib.SMTP_SSL('smtp.gmail.com', 465)
    s.login(user, pwd)
    s.sendmail(user, [to_email], msg.as_string())
    s.quit()

# Create DB and default users
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', password_hash=generate_password_hash('admin123'), role='admin')
        db.session.add(admin)
    if not User.query.filter_by(username='hospital').first():
        h = User(username='hospital', password_hash=generate_password_hash('hospital123'), role='hospital')
        db.session.add(h)
    db.session.commit()

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            error = "Invalid username or password."
        else:
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_role'] = user.role

            # Redirect according to role
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            elif user.role == 'hospital':
                return redirect(url_for('request_blood'))
            else:
                # default landing for other roles (if any)
                return redirect(url_for('index'))
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    # clear session so user is fully logged out
    session_keys = list(session.keys())
    for k in session_keys:
        session.pop(k, None)

    flash("You have been logged out.", "info")
    return redirect(url_for('index'))


@app.route('/register_donor', methods=['GET','POST'])
def register_donor():
    error = None
    if request.method == 'POST':
        name = request.form['name'].strip()
        blood_group = request.form['blood_group'].strip()
        phone = request.form['phone'].strip()
        try:
            lat = float(request.form['latitude'])
            lon = float(request.form['longitude'])
        except:
            error = "Invalid latitude/longitude."
            return render_template('register_donor.html', error=error)
        if Donor.query.filter_by(phone=phone).first():
            error = "Phone already registered."
            return render_template('register_donor.html', error=error)
        d = Donor(name=name, blood_group=blood_group, phone=phone,
                  latitude=lat, longitude=lon, is_available=True)
        db.session.add(d)
        db.session.commit()
        flash("Donor registered successfully!", "success")
        return redirect(url_for('index'))
    return render_template('register_donor.html', error=error)

@app.route('/request_blood', methods=['GET','POST'])
@login_required(role='hospital')
def request_blood():
    error = None
    if request.method == 'POST':
        patient_name = request.form['patient_name'].strip()
        required_blood_group = request.form['required_blood_group'].strip()
        try:
            lat = float(request.form['latitude'])
            lon = float(request.form['longitude'])
        except:
            error = "Invalid latitude/longitude."
            return render_template('request_blood.html', error=error)
        req = BloodRequest(patient_name=patient_name, required_blood_group=required_blood_group,
                           latitude=lat, longitude=lon, created_by=session.get('user_id'))
        db.session.add(req)
        db.session.commit()
        donors = find_best_donors(required_blood_group, lat, lon, max_results=5)
        for donor, dist, score in donors:
            msg = f"Emergency: blood {required_blood_group} needed near you for patient {patient_name}. Pls respond."
            # production: send_sms_twilio(donor.phone, msg)
            send_sms_mock(donor.phone, msg)
            send_email_smtp(f"{donor.phone}@example.com", "RapidRed Emergency", msg)
        flash(f"Notifications sent to {len(donors)} donors.", "info")
        return render_template('results.html', donors=donors, request=req)
    return render_template('request_blood.html', error=error)

# Admin - donors
@app.route('/donors')
@login_required(role='admin')
def donors():
    all_donors = Donor.query.all()
    return render_template('donors.html', donors=all_donors)

@app.route('/delete_donor/<int:donor_id>')
@login_required(role='admin')
def delete_donor(donor_id):
    d = Donor.query.get(donor_id)
    if d:
        db.session.delete(d)
        db.session.commit()
    return redirect(url_for('donors'))

# Admin dashboard and map
@app.route('/admin/dashboard')
@login_required(role='admin')
def admin_dashboard():
    total_donors = Donor.query.count()
    total_requests = BloodRequest.query.count()
    open_requests = BloodRequest.query.filter_by(status="OPEN").count()
    recent_requests = BloodRequest.query.order_by(BloodRequest.created_at.desc()).limit(5).all()
    return render_template('admin_dashboard.html', total_donors=total_donors,
                           total_requests=total_requests, open_requests=open_requests,
                           recent_requests=recent_requests)

@app.route('/admin/map')
@login_required(role='admin')
def admin_map():
    return render_template('dashboard_map.html')

@app.route('/api/all_donors')
@login_required(role='admin')
def api_all_donors():
    donors = Donor.query.all()
    out = []
    for d in donors:
        out.append({
            "id": d.id,
            "name": d.name,
            "phone": d.phone,
            "blood_group": d.blood_group,
            "latitude": d.latitude,
            "longitude": d.longitude,
            "last_seen": d.last_seen.isoformat() if d.last_seen else None,
            "is_online": d.is_online
        })
    return jsonify(out)

# SocketIO events
@socketio.on('donor_location_update')
def handle_donor_location(data):
    phone = data.get('phone')
    lat = data.get('lat')
    lon = data.get('lon')
    if not phone:
        return
    donor = Donor.query.filter_by(phone=phone).first()
    if donor:
        try:
            donor.latitude = float(lat)
            donor.longitude = float(lon)
        except:
            pass
        donor.last_seen = datetime.utcnow()
        donor.is_online = True
        db.session.commit()
        emit('donor_updated', {
            "id": donor.id,
            "name": donor.name,
            "phone": donor.phone,
            "blood_group": donor.blood_group,
            "latitude": donor.latitude,
            "longitude": donor.longitude,
            "last_seen": donor.last_seen.isoformat(),
            "is_online": donor.is_online
        }, broadcast=True)

@socketio.on('donor_stop_sharing')
def handle_stop_sharing(data):
    phone = data.get('phone')
    donor = Donor.query.filter_by(phone=phone).first()
    if donor:
        donor.is_online = False
        db.session.commit()
        emit('donor_offline', {"id": donor.id}, broadcast=True)

if __name__ == '__main__':
    # For local dev use socketio.run
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)
