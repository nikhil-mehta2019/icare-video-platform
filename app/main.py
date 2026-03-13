from fastapi import FastAPI
from app.routes import playback, batch, courses, videos,migration
from app.database.db import Base, engine


app = FastAPI()

Base.metadata.create_all(bind=engine)

app.include_router(playback.router)
app.include_router(batch.router)
app.include_router(courses.router)
app.include_router(videos.router)
app.include_router(migration.router)

@app.get("/")
def home():
    return {"message": "iCare Video Platform Running"}