from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import settings


class Base(DeclarativeBase):
    pass


connect_args: dict = {}
engine_kwargs: dict = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}
else:
    connect_args = {"connect_timeout": 20}
    engine_kwargs = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_recycle": 280,
    }

engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _timestamp_type() -> str:
    return "DATETIME" if settings.database_url.startswith("sqlite") else "TIMESTAMPTZ"


ASSET_COLUMN_SQL = {
    "source_url": "ALTER TABLE assets ADD COLUMN source_url VARCHAR(1000) DEFAULT ''",
    "file_key": "ALTER TABLE assets ADD COLUMN file_key VARCHAR(500) DEFAULT ''",
    "content_type": "ALTER TABLE assets ADD COLUMN content_type VARCHAR(128) DEFAULT ''",
    "matrix_ref": "ALTER TABLE assets ADD COLUMN matrix_ref VARCHAR(16) DEFAULT ''",
    "file_status": "ALTER TABLE assets ADD COLUMN file_status VARCHAR(16) DEFAULT 'pending'",
    "sync_error": "ALTER TABLE assets ADD COLUMN sync_error TEXT DEFAULT ''",
    "synced_at": f"ALTER TABLE assets ADD COLUMN synced_at {_timestamp_type()}",
    "extract_status": "ALTER TABLE assets ADD COLUMN extract_status VARCHAR(16) DEFAULT ''",
    "extract_error": "ALTER TABLE assets ADD COLUMN extract_error TEXT DEFAULT ''",
    "extract_json": "ALTER TABLE assets ADD COLUMN extract_json TEXT DEFAULT ''",
    "extracted_at": f"ALTER TABLE assets ADD COLUMN extracted_at {_timestamp_type()}",
}

GENERATION_COLUMN_SQL = {
    "founder_quote_ids": "ALTER TABLE generations ADD COLUMN founder_quote_ids VARCHAR(255) DEFAULT ''",
    "report_asset_ids": "ALTER TABLE generations ADD COLUMN report_asset_ids VARCHAR(255) DEFAULT ''",
    "report_passages_json": "ALTER TABLE generations ADD COLUMN report_passages_json TEXT DEFAULT ''",
}


def _add_missing_columns(table: str, statements: dict[str, str]) -> None:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    with engine.begin() as connection:
        for name, statement in statements.items():
            if name not in existing:
                connection.execute(text(statement))


def ensure_schema() -> None:
    _add_missing_columns("assets", ASSET_COLUMN_SQL)
    _add_missing_columns("generations", GENERATION_COLUMN_SQL)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
