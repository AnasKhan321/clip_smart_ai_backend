import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clipforge.db")

_is_sqlite = "sqlite" in DATABASE_URL

# pool_pre_ping: test connection with a tiny SELECT 1 before handing it out;
# recovers from Supabase session pooler closing idle connections (~10min).
# pool_recycle: proactively recycle connections older than 5 min to avoid
# the same idle-timeout class of bugs.
_engine_kwargs = {}
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs.update(
        pool_pre_ping=True,
        pool_recycle=300,
    )

engine = create_engine(DATABASE_URL, **_engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()


# Columns added after initial schema. Idempotent: safe to run on every startup.
_PENDING_COLUMNS = [
    ("clips", "error_message", "VARCHAR"),
    ("jobs", "source_width", "INTEGER"),
    ("jobs", "source_height", "INTEGER"),
    ("jobs", "r2_source_key", "VARCHAR"),
    ("clips", "r2_clip_key", "VARCHAR"),
    ("users", "is_email_verified", "BOOLEAN DEFAULT FALSE"),
]


def _apply_lightweight_migrations():
    """Add columns introduced after initial table creation.

    PostgreSQL supports ADD COLUMN IF NOT EXISTS. SQLite ignores; we check first.
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, col, coltype in _PENDING_COLUMNS:
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if col in existing:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {coltype}'))
