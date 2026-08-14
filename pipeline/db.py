from __future__ import annotations

import os
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def get_database_url() -> str:
    return os.getenv("UFC_DATABASE_URL", "sqlite:///./ufc_fights.db")


def get_engine(database_url: str | None = None) -> Engine:
    url = (database_url or get_database_url()).strip()
    engine_kwargs = {"future": True}
    if url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **engine_kwargs)


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db(database_url: str | None = None) -> Engine:
    global engine, SessionLocal
    engine = get_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    Base.metadata.create_all(bind=engine)
    return engine


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
