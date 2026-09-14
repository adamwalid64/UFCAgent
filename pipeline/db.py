from __future__ import annotations

import os
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .migrations import upgrade_schema


def get_database_url() -> str:
    return os.getenv("UFC_DATABASE_URL", "sqlite:///./ufc_fights.db")


def get_engine(database_url: str | None = None) -> Engine:
    url = (database_url or get_database_url()).strip()
    engine_kwargs = {"future": True}
    if url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    database_engine = create_engine(url, **engine_kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(database_engine, "connect")
        def _enable_sqlite_integrity(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return database_engine


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db(database_url: str | None = None) -> Engine:
    global engine, SessionLocal
    engine = get_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    Base.metadata.create_all(bind=engine)
    upgrade_schema(engine)
    return engine


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
