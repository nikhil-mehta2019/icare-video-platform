from app.database.session import engine
from app.database.models import Base

print("Dropping all tables...")
Base.metadata.drop_all(bind=engine)

print("Creating tables...")
Base.metadata.create_all(bind=engine)

print("Database reset complete.")