"""SQLite database and schema for runs, results, overrides, and expected data."""
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, Session, sessionmaker

Base = declarative_base()

DB_PATH = os.environ.get("IDP_VALIDATION_DB", str(Path(__file__).resolve().parent.parent / "data" / "idp_validation.db"))


def get_engine():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create tables if they do not exist. Never drops tables or deletes rows — safe on every app startup."""
    from app import models  # noqa: F401 - ensure models are registered
    Base.metadata.create_all(bind=engine)
    # Ensure idp_credentials exists (in case DB predates the model)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS idp_credentials (id INTEGER PRIMARY KEY, credentials_json TEXT, updated_at DATETIME)"
        ))
        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
