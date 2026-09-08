from __future__ import annotations

from backend.auth import bootstrap_admin
from backend.database import Base, SessionLocal, engine, ensure_schema


def boot() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    db = SessionLocal()
    try:
        bootstrap_admin(db)
    finally:
        db.close()


if __name__ == "__main__":
    boot()
