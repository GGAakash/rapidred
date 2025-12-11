# show_db.py
from app import app
print("Using DB URI:", app.config.get('SQLALCHEMY_DATABASE_URI'))

