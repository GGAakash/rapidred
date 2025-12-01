from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from models import db, User, Donor, BloodRequest
from matching import find_best_donors

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///rapidred.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'change_this_to_a_random_secret'

db.init_app(app)


# ---------- Helper: login required ----------

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


# ---------- Notification mocks (SMS + Email) ----------

def send_sms_mock(phone, message):
    # For demo: print to console. Replace with Twilio API etc.
    print(f"[SMS to {phone}] {message}")

def send_email_mock(to, subject, body):
    # For demo: print to console. Replace with real SMTP later.
    print(f"[EMAIL to {to}] Subject: {subject}\n{body}\n")


# ---------- DB setup & default admin ----------

with app.app_context():
    db.create_all()
    # Create default admin and hospital user if not exists
    if not User.query.filter_by(username='admin').first():
        admin = User(
            username='admin',
            password_hash=generate_password_hash('admin123'),
            role='admin'
        )
        db.session.add(admin)
    if not User.query.filter_by(username='hospital').first():
        hospital = User(
            username='hospital',
            password_hash=generate_password_hash('hospital123'),
            role='hospital'
        )
        db.session.add(hospital)
    db.session.commit()


# ---------- Routes ----------

@app.route('/')
def index():
    return render_template('index.html')


# ---------- Authentication ----------

@app.route('/login', methods=['GET', 'POST'])
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

            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('index'))

    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ---------- Donor Registration (Public) ----------

@app.route('/register_donor', methods=['GET', 'POST'])
def register_donor():
    error = None
    if request.method == 'POST':
        name = request.form['name']
        blood_group = request.form['blood_group']
        phone = request.form['phone']
        lat = float(request.form['latitude'])
        lon = float(request.form['longitude'])

        existing = Donor.query.filter_by(phone=phone).first()
        if existing:
            error = "This phone number is already registered as a donor."
            return render_template('register_donor.html', error=error)

        donor = Donor(
            name=name,
            blood_group=blood_group,
            phone=phone,
            latitude=lat,
            longitude=lon
        )
        db.session.add(donor)
        db.session.commit()
        flash("Donor registered successfully!", "success")
        return redirect(url_for('index'))

    return render_template('register_donor.html', error=error)


# ---------- Blood Request (Only login hospital/admin) ----------

@app.route('/request_blood', methods=['GET', 'POST'])
@login_required(role='hospital')
def request_blood():
    error = None
    if request.method == 'POST':
        patient_name = request.form['patient_name']
        required_blood_group = request.form['required_blood_group']
        lat = float(request.form['latitude'])
        lon = float(request.form['longitude'])

        req = BloodRequest(
            patient_name=patient_name,
            required_blood_group=required_blood_group,
            latitude=lat,
            longitude=lon,
            created_by=session.get('user_id')
        )
        db.session.add(req)
        db.session.commit()

        donors = find_best_donors(required_blood_group, lat, lon, max_results=5)

        # Send mock SMS/Email to top donors
        for donor, dist, score in donors:
            msg = f"Emergency blood request for {required_blood_group} near you. Patient: {patient_name}."
            send_sms_mock(donor.phone, msg)
            send_email_mock(
                to=f"{donor.phone}@example.com",  # dummy email
                subject="RapidRed Emergency Blood Request",
                body=msg
            )

        flash(f"Found {len(donors)} compatible donors and sent notifications.", "info")
        return render_template('results.html', donors=donors, request=req)

    return render_template('request_blood.html', error=error)


# ---------- Admin: View Donors ----------

@app.route('/donors')
@login_required(role='admin')
def donors():
    all_donors = Donor.query.all()
    return render_template('donors.html', donors=all_donors)


@app.route('/delete_donor/<int:donor_id>')
@login_required(role='admin')
def delete_donor(donor_id):
    donor = Donor.query.get(donor_id)
    if donor:
        db.session.delete(donor)
        db.session.commit()
    return redirect(url_for('donors'))


# ---------- Admin Dashboard with stats ----------

@app.route('/admin/dashboard')
@login_required(role='admin')
def admin_dashboard():
    total_donors = Donor.query.count()
    total_requests = BloodRequest.query.count()
    open_requests = BloodRequest.query.filter_by(status="OPEN").count()

    recent_requests = BloodRequest.query.order_by(
        BloodRequest.created_at.desc()
    ).limit(5).all()

    return render_template(
        'admin_dashboard.html',
        total_donors=total_donors,
        total_requests=total_requests,
        open_requests=open_requests,
        recent_requests=recent_requests
    )


if __name__ == '__main__':
    app.run(debug=True)
