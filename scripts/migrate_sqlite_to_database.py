r"""Copy data from the local SQLite DB into another configured database.

Usage:
  $env:TARGET_DATABASE_URL="postgresql+psycopg://..."
  .\venv\Scripts\python.exe scripts\migrate_sqlite_to_database.py

By default this reads ./courierbridge.db and refuses to import into non-empty tables.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, normalize_database_url
from app.models import Shipment, TrackingEvent, TrackingNumber, TrackingTemplate

SOURCE_SQLITE_URL = os.environ.get("SOURCE_SQLITE_URL", "sqlite:///./courierbridge.db")
TARGET_DATABASE_URL = os.environ.get("TARGET_DATABASE_URL") or os.environ.get("COURIERBRIDGE_DATABASE_URL")
TABLES = [Shipment, TrackingNumber, TrackingEvent, TrackingTemplate]


def make_engine(url: str):
    normalized = normalize_database_url(url)
    kwargs = {"pool_pre_ping": True}
    if normalized.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(normalized, **kwargs)


def row_count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def clone_model(row):
    return row.__class__(**{column.name: getattr(row, column.name) for column in row.__table__.columns})


def main():
    if not TARGET_DATABASE_URL:
        raise SystemExit("Set TARGET_DATABASE_URL or COURIERBRIDGE_DATABASE_URL to your Supabase/Postgres URL.")
    if SOURCE_SQLITE_URL.startswith("sqlite:///"):
        source_path = Path(SOURCE_SQLITE_URL.removeprefix("sqlite:///"))
        if not source_path.exists():
            raise SystemExit(f"Source SQLite DB not found: {source_path}")

    source_engine = make_engine(SOURCE_SQLITE_URL)
    target_engine = make_engine(TARGET_DATABASE_URL)
    Base.metadata.create_all(bind=target_engine)

    SourceSession = sessionmaker(bind=source_engine)
    TargetSession = sessionmaker(bind=target_engine)

    with SourceSession() as source, TargetSession() as target:
        existing = {model.__tablename__: row_count(target, model) for model in TABLES}
        non_empty = {name: count for name, count in existing.items() if count}
        if non_empty and os.environ.get("ALLOW_NON_EMPTY_IMPORT") != "true":
            raise SystemExit(
                "Target DB is not empty. Set ALLOW_NON_EMPTY_IMPORT=true only if you understand duplicates may be created: "
                + repr(non_empty)
            )

        for model in TABLES:
            rows = source.query(model).order_by(model.id).all()
            target.add_all(clone_model(row) for row in rows)
            target.flush()
            print(f"Copied {len(rows)} rows into {model.__tablename__}")
        target.commit()

    print("Migration complete.")


if __name__ == "__main__":
    main()
