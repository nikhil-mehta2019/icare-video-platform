from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool, StaticPool
from app.config import DATABASE_URL

# SQLite is a local file — connection pooling only causes contention under concurrent load.
# NullPool: each SessionLocal() call opens a fresh connection and closes it immediately on release.
# StaticPool fallback for in-memory SQLite (e.g. tests).
if DATABASE_URL.startswith("sqlite"):
    if ":memory:" in DATABASE_URL:
        engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
else:
    # PostgreSQL / MySQL — keep a real pool
    engine = create_engine(
        DATABASE_URL,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()