import os

from app.database.db import Base, engine
from app.database import models  # IMPORTANT: this loads the models

DB_FILE = "icare.db"

# Delete old database
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
    print("Old database deleted")

# Create tables
Base.metadata.create_all(bind=engine)

print("Tables created successfully")