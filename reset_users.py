# reset_users.py
from app import app, db, User

USERNAME = "hospital"
PASSWORD = "hospital123"

with app.app_context():
    # ensure tables exist
    db.create_all()

    u = User.query.filter_by(username=USERNAME).first()
    if not u:
        u = User(username=USERNAME, role="hospital")
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.commit()
        print(f"[OK] Created user '{USERNAME}' with password '{PASSWORD}'")
    else:
        u.set_password(PASSWORD)
        db.session.commit()
        print(f"[OK] Reset password for user '{USERNAME}' to '{PASSWORD}'")

    print("\nCurrent users in DB:")
    for user in User.query.order_by(User.id).all():
        print(user.id, user.username, user.role)
