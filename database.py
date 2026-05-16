import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clipforge.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

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
