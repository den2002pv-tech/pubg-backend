"""Full PostgreSQL backup and restore for the local SQLite database."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from db_sqlite_schema import SNAPSHOT_TABLES


def _sqlite_path(db_path: str | None = None) -> str:
    if db_path:
        return db_path
    from database_manager import _sqlite_path as configured_path
    return configured_path()


def _postgres_url(database_url: str | None = None) -> str:
    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    if not url.strip():
        raise RuntimeError("DATABASE_URL не задан")
    return url.strip()


def _sqlite_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _postgres_value(table: str, column: str, value):
    if table == "user_analytics_events" and column == "properties" and isinstance(value, str):
        return Jsonb(json.loads(value))
    return value


def backup_snapshot_to_pg(db_path: str | None = None, database_url: str | None = None) -> int:
    """Replace the configured PostgreSQL user tables from one SQLite snapshot."""
    sqlite_db = _sqlite_path(db_path)
    with sqlite3.connect(sqlite_db, timeout=15) as local:
        local.row_factory = sqlite3.Row
        local.execute("BEGIN")
        snapshot = {
            table: [dict(row) for row in local.execute(f'SELECT * FROM "{table}"')]
            for table in SNAPSHOT_TABLES
        }

    total_rows = sum(len(rows) for rows in snapshot.values())
    with psycopg.connect(_postgres_url(database_url), row_factory=dict_row) as pg:
        for table in SNAPSHOT_TABLES:
            pg.execute(f'DROP TRIGGER IF EXISTS "trg_app_outbox_{table}" ON "{table}"')
            pg.execute(f'DROP TRIGGER IF EXISTS "trg_app_outbox_no_truncate_{table}" ON "{table}"')
        for table in reversed(SNAPSHOT_TABLES):
            pg.execute(f'DELETE FROM "{table}"')
        for table in SNAPSHOT_TABLES:
            rows = snapshot[table]
            if not rows:
                continue
            columns = list(rows[0])
            names = ", ".join(f'"{name}"' for name in columns)
            placeholders = ", ".join("%s" for _ in columns)
            values = [
                tuple(_postgres_value(table, name, row[name]) for name in columns)
                for row in rows
            ]
            pg.executemany(
                f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', values
            )
    return total_rows


def restore_snapshot_from_pg(db_path: str | None = None, database_url: str | None = None) -> int:
    """Replace SQLite user tables with one PostgreSQL snapshot transaction."""
    sqlite_db = _sqlite_path(db_path)
    from db_sqlite_schema import ensure_sqlite_schema
    ensure_sqlite_schema(sqlite_db)
    with psycopg.connect(_postgres_url(database_url), row_factory=dict_row) as pg:
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        snapshot = {}
        for table in SNAPSHOT_TABLES:
            snapshot[table] = pg.execute(f'SELECT * FROM "{table}"').fetchall()

    total_rows = sum(len(rows) for rows in snapshot.values())
    os.makedirs(os.path.dirname(os.path.abspath(sqlite_db)), exist_ok=True)
    with sqlite3.connect(sqlite_db, timeout=15) as local:
        local.execute("PRAGMA foreign_keys=ON")
        for table in reversed(SNAPSHOT_TABLES):
            local.execute(f'DELETE FROM "{table}"')
        for table in SNAPSHOT_TABLES:
            rows = snapshot[table]
            if not rows:
                continue
            columns = list(rows[0])
            names = ", ".join(f'"{name}"' for name in columns)
            placeholders = ", ".join("?" for _ in columns)
            local.executemany(
                f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({placeholders})',
                [tuple(_sqlite_value(row[name]) for name in columns) for row in rows],
            )
    return total_rows
