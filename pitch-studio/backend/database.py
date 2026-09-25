from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import settings


class Base(DeclarativeBase):
    pass


connect_args: dict = {}
engine_kwargs: dict = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}
else:
    connect_args = {"connect_timeout": 8}
    engine_kwargs = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_recycle": 280,
    }

engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)

_DISCONNECT_MARKERS = (
    "could not receive data from server",
    "server closed the connection unexpectedly",
    "ssl syscall error",
    "connection reset by peer",
    "connection is closed",
    "terminating connection",
    "broken pipe",
)


def _is_network_disconnect(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _DISCONNECT_MARKERS)


if not settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "handle_error")
    def _invalidate_network_disconnect(context) -> None:  # noqa: ANN001
        """Keep a failed PostgreSQL socket from returning to QueuePool."""
        if _is_network_disconnect(context.original_exception):
            context.is_disconnect = True


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
    "cache_key": "ALTER TABLE generations ADD COLUMN cache_key VARCHAR(64) DEFAULT ''",
    "cached_from_id": "ALTER TABLE generations ADD COLUMN cached_from_id INTEGER",
    "deck_use_case": "ALTER TABLE generations ADD COLUMN deck_use_case VARCHAR(64) DEFAULT ''",
    "script_plan_json": "ALTER TABLE generations ADD COLUMN script_plan_json TEXT DEFAULT ''",
    "quality_trace_json": "ALTER TABLE generations ADD COLUMN quality_trace_json TEXT DEFAULT ''",
}

OBJECTION_COLUMN_SQL = {
    "source_name": "ALTER TABLE objections ADD COLUMN source_name VARCHAR(300) DEFAULT ''",
    "source_transcript_id": "ALTER TABLE objections ADD COLUMN source_transcript_id INTEGER DEFAULT 0",
    "source_candidate_id": "ALTER TABLE objections ADD COLUMN source_candidate_id INTEGER DEFAULT 0",
}

FOUNDER_QUOTE_COLUMN_SQL = {
    "source_style_transcript_id": (
        "ALTER TABLE founder_quotes ADD COLUMN source_style_transcript_id INTEGER DEFAULT 0"
    ),
}

VOICE_STYLE_GUIDE_COLUMN_SQL = {
    "persona_label": "ALTER TABLE voice_style_guides ADD COLUMN persona_label VARCHAR(255) DEFAULT ''",
}

STYLE_TRANSCRIPT_COLUMN_SQL = {
    "source_url": "ALTER TABLE style_transcripts ADD COLUMN source_url VARCHAR(1000) DEFAULT ''",
}

SCRIPT_TEST_RUN_COLUMN_SQL = {
    "deck_use_case": "ALTER TABLE script_test_runs ADD COLUMN deck_use_case VARCHAR(64) DEFAULT ''",
    "script_plan_json": "ALTER TABLE script_test_runs ADD COLUMN script_plan_json TEXT DEFAULT ''",
    "quality_trace_json": "ALTER TABLE script_test_runs ADD COLUMN quality_trace_json TEXT DEFAULT ''",
}

DECK_TOPIC_COLUMN_SQL = {
    "deck": "ALTER TABLE deck_topics ADD COLUMN deck VARCHAR(16) DEFAULT 'brand'",
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


def _ensure_generation_cache_key_index() -> None:
    """CREATE INDEX for Generation.cache_key on DBs that only got ALTER TABLE."""
    inspector = inspect(engine)
    if "generations" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("generations")}
    if "cache_key" not in columns:
        return
    for index in inspector.get_indexes("generations"):
        if list(index.get("column_names") or []) == ["cache_key"]:
            return
    with engine.begin() as connection:
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_generations_cache_key ON generations (cache_key)")
        )


def _backfill_founder_quote_style_provenance() -> None:
    """Fill source_style_transcript_id from legacy source_file_id=style-{id}."""
    inspector = inspect(engine)
    if "founder_quotes" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("founder_quotes")}
    if "source_style_transcript_id" not in columns:
        return
    with engine.begin() as connection:
        if settings.database_url.startswith("sqlite"):
            connection.execute(
                text(
                    """
                    UPDATE founder_quotes
                    SET source_style_transcript_id = CAST(
                        substr(source_file_id, 7) AS INTEGER
                    )
                    WHERE COALESCE(source_style_transcript_id, 0) = 0
                      AND source_file_id LIKE 'style-%'
                      AND length(source_file_id) > 6
                      AND substr(source_file_id, 7) GLOB '[0-9]*'
                    """
                )
            )
        else:
            connection.execute(
                text(
                    """
                    UPDATE founder_quotes
                    SET source_style_transcript_id = CAST(
                        substring(source_file_id FROM 7) AS INTEGER
                    )
                    WHERE COALESCE(source_style_transcript_id, 0) = 0
                      AND source_file_id ~ '^style-[0-9]+$'
                    """
                )
            )


_SCHEMA_READY = False


def ensure_schema() -> None:
    # create_all is checkfirst=True; it only builds tables that are missing
    # (media_index and any later models). Column backfills stay explicit.
    # Cache the result: remote Postgres inspect/create_all is multi-second work
    # and was previously re-run on every deck-topics / media list request.
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    from backend import models as _models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _add_missing_columns("assets", ASSET_COLUMN_SQL)
    _add_missing_columns("generations", GENERATION_COLUMN_SQL)
    _add_missing_columns("objections", OBJECTION_COLUMN_SQL)
    _add_missing_columns("founder_quotes", FOUNDER_QUOTE_COLUMN_SQL)
    _add_missing_columns("voice_style_guides", VOICE_STYLE_GUIDE_COLUMN_SQL)
    _add_missing_columns("style_transcripts", STYLE_TRANSCRIPT_COLUMN_SQL)
    _add_missing_columns("deck_topics", DECK_TOPIC_COLUMN_SQL)
    _add_missing_columns("script_test_runs", SCRIPT_TEST_RUN_COLUMN_SQL)
    _ensure_generation_cache_key_index()
    _backfill_founder_quote_style_provenance()
    _SCHEMA_READY = True


def reset_schema_cache() -> None:
    """Test helper — force the next ensure_schema() to run again."""
    global _SCHEMA_READY
    _SCHEMA_READY = False


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    except DBAPIError:
        # Discard every connection held by this Session after a driver error;
        # a broken socket must never be checked back into QueuePool.
        db.invalidate()
        raise
    finally:
        try:
            db.close()
        except DBAPIError:
            # A dead socket may only reveal itself during the implicit rollback
            # performed by close(). Invalidate it instead of leaking it.
            db.invalidate()
