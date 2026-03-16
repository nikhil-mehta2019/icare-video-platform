from fastapi import FastAPI
from app.routes import playback, videos, migration, webhook
from app.database.session import Base, engine

app = FastAPI(title="iCare Video Migration Platform")

# Create tables based on the updated models
Base.metadata.create_all(bind=engine)

app.include_router(playback.router)
app.include_router(videos.router)
app.include_router(migration.router)
app.include_router(webhook.router)  # Added webhook support

@app.get("/")
def home():
    return {"message": "iCare Video Migration Infrastructure Running"}