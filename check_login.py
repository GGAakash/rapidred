# check_login.py
from app import app, User
with app.app_context():
    u = User.query.filter_by(username="hospital").first()
    if not u:
        print("User not found: hospital")
    else:
        print("Found user:", u.username, "role:", u.role)
        print("Password check (hospital123):", u.check_password("hospital123"))
