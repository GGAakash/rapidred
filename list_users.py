# list_users.py
from app import app, db
from app import User
with app.app_context():
    users = User.query.all()
    if not users:
        print("No users found.")
    else:
        for u in users:
            print(u.id, u.username, u.role)
