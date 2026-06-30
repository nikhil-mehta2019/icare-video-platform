from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool, StaticPool
from app.config import DATABASE_URL

# SQLite: use NullPool so there is no connection pool ceiling.
# Each SessionLocal() opens its own connection and closes it on release —
# eliminating "QueuePool limit reached" errors under concurrent webhook load.
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

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")   # concurrent readers + one writer
            cur.execute("PRAGMA busy_timeout=30000") # wait up to 30s instead of failing
            cur.close()
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
