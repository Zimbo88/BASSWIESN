"""SQLAlchemy engine, sessions, and application database lifecycle."""

from collections.abc import Generator
from pathlib import Path
import fcntl

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from basswiesn.app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    get_settings().database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    from basswiesn.app.db.migrations import ensure_schema_baseline

    lock_path = Path("data/.db-init.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        ensure_schema_baseline(engine)
        fcntl.flock(lock_file, fcntl.LOCK_UN)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
