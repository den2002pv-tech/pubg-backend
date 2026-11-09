"""PostgreSQL-primary data access; SQLite is updated only by the PG Outbox consumer."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from database_manager import DatabaseMode, get_manager
from db_sqlite_schema import (
    card_id_order_sql_sqlite,
    desired_cards_order_sql_sqlite,
    in_placeholders,
    adapt_sqlite_query,
    now_sqlite,
    parse_properties,
    row_to_dict,
    rows_to_dicts,
    setup_sqlite_functions,
)

# ---------------------------------------------------------------------------
# Метрики (сохранены для обратной совместимости)
# ---------------------------------------------------------------------------

_DB_METRICS: ContextVar[dict | None] = ContextVar("db_metrics", default=None)
_CURRENT_PG_CONNECTION: ContextVar[psycopg.Connection | None] = ContextVar(
    "current_pg_connection", default=None
)


@contextmanager
def measure_database(metrics: dict):
    token = _DB_METRICS.set(metrics)
    try:
        yield
    finally:
        _DB_METRICS.reset(token)


def _add_metric(name: str, value: float):
    metrics = _DB_METRICS.get()
    if metrics is not None:
        metrics[name] = metrics.get(name, 0) + value


# ---------------------------------------------------------------------------
# Получение соединения (роутер)
# ---------------------------------------------------------------------------

@contextmanager
def get_connection():
    """Yield active connection: PostgreSQL if available, or SQLite in fallback mode."""
    mgr = get_manager()
    if not mgr.is_postgres:
        with _sqlite_connection_ctx() as conn:
            yield conn
        return

    current = _CURRENT_PG_CONNECTION.get()
    if current is not None:
        savepoint = f"nested_{uuid.uuid4().hex}"
        current.execute(f"SAVEPOINT {savepoint}")
        try:
            yield _PGConnectionWrapper(current, mgr, owns_transaction=False)
        except BaseException:
            try:
                current.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                current.execute(f"RELEASE SAVEPOINT {savepoint}")
            except (psycopg.Error, psycopg.InterfaceError):
                pass
            raise
        else:
            current.execute(f"RELEASE SAVEPOINT {savepoint}")
        return

    started = time.perf_counter()
    try:
        conn = psycopg.connect(
            os.getenv("DATABASE_URL", ""),
            row_factory=dict_row,
            connect_timeout=max(1, int(float(os.getenv("DB_FAILOVER_CONNECT_TIMEOUT", "5")))),
            options="-c statement_timeout=10000 -c lock_timeout=3000",
        )
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        mgr.notify_pg_write_failed(exc)
        if mgr._sqlite_has_schema():
            mgr._set_mode(DatabaseMode.SQLITE, f"PG connection failed: {exc}")
            with _sqlite_connection_ctx() as fallback_conn:
                yield fallback_conn
            return
        raise
    _add_metric("db_connection_ms", (time.perf_counter() - started) * 1000)
    _add_metric("db_connection_count", 1)
    tx_start = time.perf_counter()
    token = _CURRENT_PG_CONNECTION.set(conn)
    try:
        with conn:
            yield _PGConnectionWrapper(conn, mgr)
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        mgr.notify_pg_write_failed(exc)
        if mgr._sqlite_has_schema():
            mgr._set_mode(DatabaseMode.SQLITE, f"PG transaction failed: {exc}")
            with _sqlite_connection_ctx() as fallback_conn:
                yield fallback_conn
            return
        raise
    finally:
        _CURRENT_PG_CONNECTION.reset(token)
        conn.close()
        _add_metric("db_transaction_ms", (time.perf_counter() - tx_start) * 1000)


@contextmanager
def _sqlite_connection_ctx():
    """Контекст SQLite-соединения с адаптером."""
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        setup_sqlite_functions(con, outbox_enabled=mgr.mode == DatabaseMode.SQLITE)
        yield _SQLiteConnectionAdapter(con)


# ---------------------------------------------------------------------------
# Обёртки для совместимости интерфейса psycopg / sqlite3
# ---------------------------------------------------------------------------

class _PGConnectionWrapper:
    """Psycopg connection facade that preserves outer transaction boundaries."""

    def __init__(self, conn: psycopg.Connection, mgr, owns_transaction: bool = True) -> None:
        self._conn = conn
        self._mgr = mgr
        self._owns_transaction = owns_transaction

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        if self._owns_transaction:
            return self._conn.commit()
        return None

    def rollback(self):
        if self._owns_transaction:
            return self._conn.rollback()
        return None

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)


class _SQLiteCursor:
    """Курсор-адаптер для sqlite3, совместимый с psycopg Cursor API."""

    def __init__(self, cur: sqlite3.Cursor, con: sqlite3.Connection) -> None:
        self._cur = cur
        self._con = con
        self.rowcount = -1
        self.lastrowid = None

    def execute(self, query: str, params=None):
        t = time.perf_counter()
        try:
            query, lock_requested = adapt_sqlite_query(query)
            if lock_requested and not self._con.in_transaction:
                self._con.execute("BEGIN IMMEDIATE")
            if params is None:
                self._cur.execute(query)
            else:
                self._cur.execute(query, params)
            self.rowcount = self._cur.rowcount
            self.lastrowid = self._cur.lastrowid
            _add_metric("db_sql_ms", (time.perf_counter() - t) * 1000)
            _add_metric("db_sql_count", 1)
            return self
        except Exception:
            _add_metric("db_sql_ms", (time.perf_counter() - t) * 1000)
            _add_metric("db_sql_count", 1)
            raise

    def fetchone(self):
        row = self._cur.fetchone()
        return row_to_dict(row) if row is not None else None

    def fetchall(self):
        rows = self._cur.fetchall()
        return rows_to_dicts(rows)

    def __iter__(self):
        return (row_to_dict(r) for r in self._cur)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _SQLiteConnectionAdapter:
    """Адаптер sqlite3.Connection под psycopg.Connection API."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        self._cursor_obj = None

    def cursor(self):
        raw = self._con.cursor()
        self._cursor_obj = _SQLiteCursor(raw, self._con)
        return self._cursor_obj

    def commit(self):
        self._con.commit()

    def rollback(self):
        self._con.rollback()

    def execute(self, query: str, params=None):
        raw = self._con.cursor()
        cur = _SQLiteCursor(raw, self._con)
        cur.execute(query, params)
        return cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._con.rollback()
        else:
            self._con.commit()
        return False


# ---------------------------------------------------------------------------
# Outbox-хелпер
# ---------------------------------------------------------------------------

def _record_outbox(op_type: str, payload: dict, sync_status: str = "pending") -> str:
    """Legacy compatibility hook; PostgreSQL row triggers own Stage 1 journaling."""
    return ""


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Создать таблицы если не существуют. Работает с активной БД."""
    mgr = get_manager()
    if mgr.is_postgres:
        _init_db_pg()


def _init_db_pg() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ,
                    last_activity_at TIMESTAMPTZ
                );

                ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT '';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT NOT NULL DEFAULT '';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT NOT NULL DEFAULT '';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;
                CREATE INDEX IF NOT EXISTS idx_users_last_activity_at ON users (last_activity_at DESC);

                CREATE TABLE IF NOT EXISTS cards (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    rarity INTEGER NOT NULL DEFAULT 1,
                    icon TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS user_cards (
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                    PRIMARY KEY (user_id, card_id)
                );

                CREATE TABLE IF NOT EXISTS desired_cards (
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, card_id)
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id BIGSERIAL PRIMARY KEY,
                    from_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    to_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
        conn.commit()
        from db_sync import ensure_postgres_outbox_schema
        ensure_postgres_outbox_schema(conn)
        conn.commit()


# ---------------------------------------------------------------------------
# get_or_create_user
# ---------------------------------------------------------------------------

def get_or_create_user(
    telegram_id: int,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
) -> dict:
    """Создать/обновить пользователя и вернуть его данные."""
    payload = {
        "telegram_id": int(telegram_id),
        "username": username.strip().lstrip("@"),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
    }
    return _get_or_create_user_pg(payload)


def _get_or_create_user_pg(payload: dict) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name, last_seen_at, last_activity_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = COALESCE(NULLIF(EXCLUDED.username, ''), users.username),
                    first_name = COALESCE(NULLIF(EXCLUDED.first_name, ''), users.first_name),
                    last_name = COALESCE(NULLIF(EXCLUDED.last_name, ''), users.last_name),
                    last_seen_at = NOW(),
                    last_activity_at = NOW()
                RETURNING id, telegram_id, username, first_name, last_name, created_at,
                          last_seen_at, last_activity_at
                """,
                (payload["telegram_id"], payload["username"],
                 payload["first_name"], payload["last_name"]),
            )
            user = cur.fetchone()
        conn.commit()
    return dict(user)


def _ts_to_str(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(val)


def _mark_outbox_synced(op_id: str) -> None:
    # Kept for callers from the earlier typed-outbox implementation.
    return None


# ---------------------------------------------------------------------------
# get_admin_users
# ---------------------------------------------------------------------------

def get_admin_users(search: str = "", online_seconds: int = 90, limit: int = 50, offset: int = 0) -> dict:
    search = (search or "").strip()[:200]
    online_seconds = max(30, min(int(online_seconds), 600))
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    mgr = get_manager()
    if mgr.is_postgres:
        return _get_admin_users_pg(search, online_seconds, limit, offset)
    else:
        return _get_admin_users_sqlite(search, online_seconds, limit, offset)


def _get_admin_users_pg(search: str, online_seconds: int, limit: int, offset: int) -> dict:
    pattern = f"%{search}%"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE last_activity_at >= NOW() - (%s * INTERVAL '1 second')) AS online
                   FROM users
                   WHERE (%s = '' OR telegram_id::text ILIKE %s OR username ILIKE %s OR first_name ILIKE %s OR last_name ILIKE %s)""",
                (online_seconds, search, pattern, pattern, pattern, pattern),
            )
            summary = cur.fetchone()
            cur.execute(
                """SELECT u.id, u.telegram_id, u.username, u.first_name, u.last_name,
                          u.created_at, u.last_seen_at, u.last_activity_at,
                          (u.last_activity_at >= NOW() - (%s * INTERVAL '1 second')) AS is_online,
                          COALESCE(uc.owned_cards, 0) AS owned_cards,
                          COALESCE(uc.duplicates, 0) AS duplicates,
                          COALESCE(dc.wanted_cards, 0) AS wanted_cards
                   FROM users u
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) FILTER (WHERE quantity > 0) AS owned_cards,
                                     SUM(GREATEST(quantity - 1, 0)) AS duplicates
                              FROM user_cards GROUP BY user_id) uc ON uc.user_id = u.id
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) AS wanted_cards
                              FROM desired_cards GROUP BY user_id) dc ON dc.user_id = u.id
                   WHERE (%s = '' OR u.telegram_id::text ILIKE %s OR u.username ILIKE %s OR u.first_name ILIKE %s OR u.last_name ILIKE %s)
                   ORDER BY u.last_activity_at DESC NULLS LAST, u.created_at DESC, u.id DESC
                   LIMIT %s OFFSET %s""",
                (online_seconds, search, pattern, pattern, pattern, pattern, limit, offset),
            )
            users = [dict(row) for row in cur.fetchall()]
    return {"users": users, "total": int(summary["total"]), "online": int(summary["online"]),
            "limit": limit, "offset": offset, "online_seconds": online_seconds}


def _get_admin_users_sqlite(search: str, online_seconds: int, limit: int, offset: int) -> dict:
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=online_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pattern = f"%{search}%"
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        setup_sqlite_functions(con)
        # Фильтр поиска
        if search:
            where = "WHERE (CAST(u.telegram_id AS TEXT) LIKE ? OR u.username LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ?)"
            search_params = [pattern, pattern, pattern, pattern]
        else:
            where = ""
            search_params = []

        total_row = con.execute(
            f"SELECT COUNT(*) AS total FROM users u {where}",
            search_params,
        ).fetchone()
        total = int(total_row["total"]) if total_row else 0

        online_row = con.execute(
            f"SELECT COUNT(*) AS online FROM users u {where} {'AND' if where else 'WHERE'} u.last_activity_at >= ?",
            search_params + [cutoff],
        ).fetchone()
        online = int(online_row["online"]) if online_row else 0

        order_null = "CASE WHEN u.last_activity_at IS NULL THEN 1 ELSE 0 END"
        rows = con.execute(
            f"""SELECT u.id, u.telegram_id, u.username, u.first_name, u.last_name,
                       u.created_at, u.last_seen_at, u.last_activity_at,
                       CASE WHEN u.last_activity_at >= ? THEN 1 ELSE 0 END AS is_online,
                       COALESCE(uc.owned_cards, 0) AS owned_cards,
                       COALESCE(uc.duplicates, 0) AS duplicates,
                       COALESCE(dc.wanted_cards, 0) AS wanted_cards
                FROM users u
                LEFT JOIN (SELECT user_id,
                                  COUNT(DISTINCT CASE WHEN quantity > 0 THEN card_id END) AS owned_cards,
                                  SUM(CASE WHEN quantity > 1 THEN quantity - 1 ELSE 0 END) AS duplicates
                           FROM user_cards GROUP BY user_id) uc ON uc.user_id = u.id
                LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) AS wanted_cards
                           FROM desired_cards GROUP BY user_id) dc ON dc.user_id = u.id
                {where}
                ORDER BY {order_null}, u.last_activity_at DESC, u.created_at DESC, u.id DESC
                LIMIT ? OFFSET ?""",
            [cutoff] + search_params + [limit, offset],
        ).fetchall()
        users = rows_to_dicts(rows)
    return {"users": users, "total": total, "online": online,
            "limit": limit, "offset": offset, "online_seconds": online_seconds}


# ---------------------------------------------------------------------------
# get_admin_user
# ---------------------------------------------------------------------------

def get_admin_user(telegram_id: int) -> dict | None:
    mgr = get_manager()
    if mgr.is_postgres:
        return _get_admin_user_pg(telegram_id)
    else:
        return _get_admin_user_sqlite(telegram_id)


def _get_admin_user_pg(telegram_id: int) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.id, u.telegram_id, u.username, u.first_name, u.last_name,
                          u.created_at, u.last_seen_at, u.last_activity_at,
                          COALESCE(uc.owned_cards, 0) AS owned_cards,
                          COALESCE(uc.duplicates, 0) AS duplicates,
                          COALESCE(dc.wanted_cards, 0) AS wanted_cards
                   FROM users u
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) FILTER (WHERE quantity > 0) AS owned_cards,
                                     SUM(GREATEST(quantity - 1, 0)) AS duplicates
                              FROM user_cards GROUP BY user_id) uc ON uc.user_id = u.id
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) AS wanted_cards
                              FROM desired_cards GROUP BY user_id) dc ON dc.user_id = u.id
                   WHERE u.telegram_id = %s""",
                (telegram_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def _get_admin_user_sqlite(telegram_id: int) -> dict | None:
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        row = con.execute(
            """SELECT u.id, u.telegram_id, u.username, u.first_name, u.last_name,
                      u.created_at, u.last_seen_at, u.last_activity_at,
                      COALESCE(uc.owned_cards, 0) AS owned_cards,
                      COALESCE(uc.duplicates, 0) AS duplicates,
                      COALESCE(dc.wanted_cards, 0) AS wanted_cards
               FROM users u
               LEFT JOIN (SELECT user_id,
                                 COUNT(DISTINCT CASE WHEN quantity > 0 THEN card_id END) AS owned_cards,
                                 SUM(CASE WHEN quantity > 1 THEN quantity - 1 ELSE 0 END) AS duplicates
                          FROM user_cards GROUP BY user_id) uc ON uc.user_id = u.id
               LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) AS wanted_cards
                          FROM desired_cards GROUP BY user_id) dc ON dc.user_id = u.id
               WHERE u.telegram_id = ?""",
            (telegram_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# set_user_card_quantity
# ---------------------------------------------------------------------------

def set_user_card_quantity(telegram_id: int, card_id: str, quantity: int) -> None:
    quantity = max(0, int(quantity))
    payload = {"telegram_id": int(telegram_id), "card_id": str(card_id), "quantity": quantity}
    _set_user_card_quantity_pg(payload)


def _set_user_card_quantity_pg(payload: dict) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_cards (user_id, card_id, quantity)
                SELECT id, %s, %s FROM users WHERE telegram_id = %s
                ON CONFLICT (user_id, card_id) DO UPDATE SET quantity = EXCLUDED.quantity
                """,
                (payload["card_id"], payload["quantity"], payload["telegram_id"]),
            )
            if payload["quantity"] > 0:
                cur.execute(
                    """DELETE FROM desired_cards
                       WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                         AND card_id = %s""",
                    (payload["telegram_id"], payload["card_id"]),
                )
        conn.commit()


# ---------------------------------------------------------------------------
# set_user_card_quantities
# ---------------------------------------------------------------------------

def set_user_card_quantities(telegram_id: int, updates: list[tuple[str, int]]) -> None:
    """Атомарно записать абсолютные количества карточек."""
    quantities: dict[str, int] = {}
    for card_id, quantity in updates:
        normalized_id = str(card_id).strip()
        if normalized_id:
            quantities[normalized_id] = max(quantities.get(normalized_id, 0), max(0, int(quantity)))
    if not quantities:
        return
    payload = {"telegram_id": int(telegram_id), "quantities": quantities}
    _set_user_card_quantities_pg(payload)


def _set_user_card_quantities_pg(payload: dict) -> None:
    quantities = payload["quantities"]
    telegram_id = payload["telegram_id"]
    card_ids = list(quantities)
    card_quantities = [quantities[k] for k in card_ids]
    owned_card_ids = [k for k, v in quantities.items() if v > 0]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_cards (user_id, card_id, quantity)
                SELECT u.id, batch.card_id, batch.quantity
                FROM users u
                CROSS JOIN unnest(%s::text[], %s::integer[]) AS batch(card_id, quantity)
                WHERE u.telegram_id = %s
                ON CONFLICT (user_id, card_id) DO UPDATE SET quantity = EXCLUDED.quantity
                """,
                (card_ids, card_quantities, telegram_id),
            )
            if owned_card_ids:
                cur.execute(
                    """DELETE FROM desired_cards
                       WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                         AND card_id = ANY(%s)""",
                    (telegram_id, owned_card_ids),
                )
        conn.commit()


# ---------------------------------------------------------------------------
# clear_user_cards
# ---------------------------------------------------------------------------

def clear_user_cards(telegram_id: int) -> int:
    """Обнулить все карточки пользователя."""
    payload = {"telegram_id": int(telegram_id)}
    return _clear_user_cards_pg(payload)


def _clear_user_cards_pg(payload: dict) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE user_cards SET quantity = 0
                   WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                     AND quantity <> 0""",
                (payload["telegram_id"],),
            )
            changed = cur.rowcount
        conn.commit()
    return changed


# ---------------------------------------------------------------------------
# get_desired_cards
# ---------------------------------------------------------------------------

def get_desired_cards(telegram_id: int) -> list[dict]:
    mgr = get_manager()
    if mgr.is_postgres:
        return _get_desired_cards_pg(telegram_id)
    else:
        return _get_desired_cards_sqlite(telegram_id)


def _get_desired_cards_pg(telegram_id: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.rarity, c.icon
                FROM desired_cards dc
                JOIN users u ON u.id = dc.user_id
                JOIN cards c ON c.id = dc.card_id
                LEFT JOIN user_cards uc ON uc.user_id = u.id AND uc.card_id = c.id
                WHERE u.telegram_id = %s
                  AND COALESCE(uc.quantity, 0) = 0
                ORDER BY
                    CASE
                        WHEN c.id ~ '^card_[0-9]+$'
                            THEN CAST(SUBSTRING(c.id FROM 6) AS INTEGER)
                        ELSE 2147483647
                    END,
                    c.id
                """,
                (telegram_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def _get_desired_cards_sqlite(telegram_id: int) -> list[dict]:
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        rows = con.execute(
            f"""
            SELECT c.id, c.name, c.rarity, c.icon
            FROM desired_cards dc
            JOIN users u ON u.id = dc.user_id
            JOIN cards c ON c.id = dc.card_id
            LEFT JOIN user_cards uc ON uc.user_id = u.id AND uc.card_id = c.id
            WHERE u.telegram_id = ?
              AND COALESCE(uc.quantity, 0) = 0
            {desired_cards_order_sql_sqlite()}
            """,
            (telegram_id,),
        ).fetchall()
    return rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# set_desired_cards
# ---------------------------------------------------------------------------

def set_desired_cards(telegram_id: int, card_ids: list[str]) -> list[dict]:
    unique_ids = list(dict.fromkeys(str(x).strip() for x in card_ids if str(x).strip()))
    payload = {"telegram_id": int(telegram_id), "card_ids": unique_ids}
    return _set_desired_cards_pg(payload)


def _set_desired_cards_pg(payload: dict) -> list[dict]:
    telegram_id = payload["telegram_id"]
    unique_ids = payload["card_ids"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM desired_cards WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)",
                (telegram_id,),
            )
            if unique_ids:
                cur.execute(
                    """
                    INSERT INTO desired_cards (user_id, card_id)
                    SELECT u.id, c.id
                    FROM users u
                    JOIN cards c ON c.id = ANY(%s)
                    LEFT JOIN user_cards uc ON uc.user_id = u.id AND uc.card_id = c.id
                    WHERE u.telegram_id = %s
                      AND COALESCE(uc.quantity, 0) = 0
                    ON CONFLICT DO NOTHING
                    """,
                    (unique_ids, telegram_id),
                )
        conn.commit()
    return get_desired_cards(telegram_id)


# ---------------------------------------------------------------------------
# get_user_cards
# ---------------------------------------------------------------------------

def get_user_cards(telegram_id: int) -> list[dict]:
    mgr = get_manager()
    if mgr.is_postgres:
        return _get_user_cards_pg(telegram_id)
    else:
        return _get_user_cards_sqlite(telegram_id)


def _get_user_cards_pg(telegram_id: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.rarity, c.icon,
                       COALESCE(uc.quantity, 0) AS quantity
                FROM cards c
                LEFT JOIN user_cards uc
                  ON uc.card_id = c.id
                 AND uc.user_id = (SELECT id FROM users WHERE telegram_id = %s)
                ORDER BY
                    CASE
                        WHEN c.id ~ '^card_[0-9]+$'
                            THEN CAST(SUBSTRING(c.id FROM 6) AS INTEGER)
                        ELSE 2147483647
                    END,
                    c.id
                """,
                (telegram_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def _get_user_cards_sqlite(telegram_id: int) -> list[dict]:
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        rows = con.execute(
            f"""
            SELECT c.id, c.name, c.rarity, c.icon,
                   COALESCE(uc.quantity, 0) AS quantity
            FROM cards c
            LEFT JOIN user_cards uc
              ON uc.card_id = c.id
             AND uc.user_id = (SELECT id FROM users WHERE telegram_id = ?)
            {card_id_order_sql_sqlite()}
            """,
            (telegram_id,),
        ).fetchall()
    return rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# add_zero_cards
# ---------------------------------------------------------------------------

def add_zero_cards(telegram_id: int, card_ids: list[str]) -> int:
    unique_ids = list(dict.fromkeys(str(card_id) for card_id in card_ids if card_id))
    if not unique_ids:
        return 0
    payload = {"telegram_id": int(telegram_id), "card_ids": unique_ids}
    return _add_zero_cards_pg(payload)


def _add_zero_cards_pg(payload: dict) -> int:
    telegram_id = payload["telegram_id"]
    unique_ids = payload["card_ids"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_cards (user_id, card_id, quantity)
                SELECT u.id, c.id, 0
                FROM users u
                JOIN cards c ON c.id = ANY(%s)
                WHERE u.telegram_id = %s
                ON CONFLICT (user_id, card_id) DO NOTHING
                """,
                (unique_ids, telegram_id),
            )
            added = cur.rowcount
        conn.commit()
    return added


# ---------------------------------------------------------------------------
# sync_cards
# ---------------------------------------------------------------------------

def sync_cards(cards: dict) -> int:
    """Синхронизировать каталог карточек из card_hashes.json."""
    valid_cards = [
        (str(card_id), info)
        for card_id, info in cards.items()
        if isinstance(info, dict)
    ]
    if not valid_cards:
        return 0
    catalogue_is_complete = len(valid_cards) == len(cards)
    active_ids = [card_id for card_id, _ in valid_cards]

    mgr = get_manager()
    return _sync_cards_pg(valid_cards, active_ids, catalogue_is_complete)


def _sync_cards_pg(valid_cards, active_ids, catalogue_is_complete: bool) -> int:
    count = 0
    mgr = get_manager()
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in valid_cards:
                name = str(info.get("name") or card_id)
                try:
                    rarity = int(info.get("rarity", 1))
                except (TypeError, ValueError):
                    rarity = 1
                icon = str(info.get("icon") or "")
                cur.execute(
                    """
                    INSERT INTO cards (id, name, rarity, icon)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        rarity = EXCLUDED.rarity,
                        icon = EXCLUDED.icon
                    """,
                    (card_id, name, rarity, icon),
                )
                count += 1
            if catalogue_is_complete and active_ids:
                if mgr.is_postgres:
                    cur.execute("DELETE FROM cards WHERE NOT (id = ANY(%s))", (active_ids,))
                else:
                    placeholders = ", ".join("?" for _ in active_ids)
                    cur.execute(f"DELETE FROM cards WHERE id NOT IN ({placeholders})", active_ids)
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# get_trade_matches
# ---------------------------------------------------------------------------

def get_trade_matches(telegram_id: int, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    mgr = get_manager()
    if mgr.is_postgres:
        return _get_trade_matches_pg(telegram_id, limit)
    else:
        return _get_trade_matches_sqlite(telegram_id, limit)


def _get_trade_matches_pg(telegram_id: int, limit: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH me AS (SELECT id FROM users WHERE telegram_id = %s),
                my_wants AS (
                    SELECT dc.card_id FROM desired_cards dc JOIN me ON me.id = dc.user_id
                    LEFT JOIN user_cards uc ON uc.user_id = dc.user_id AND uc.card_id = dc.card_id
                    WHERE COALESCE(uc.quantity, 0) = 0
                ),
                my_offers AS (
                    SELECT uc.card_id, GREATEST(uc.quantity - 1, 0) AS quantity
                    FROM user_cards uc JOIN me ON me.id = uc.user_id WHERE uc.quantity > 1
                ),
                other_wants AS (
                    SELECT dc.user_id, dc.card_id FROM desired_cards dc JOIN me ON dc.user_id <> me.id
                    LEFT JOIN user_cards uc ON uc.user_id = dc.user_id AND uc.card_id = dc.card_id
                    WHERE COALESCE(uc.quantity, 0) = 0
                ),
                other_offers AS (
                    SELECT uc.user_id, uc.card_id, GREATEST(uc.quantity - 1, 0) AS quantity
                    FROM user_cards uc JOIN me ON uc.user_id <> me.id WHERE uc.quantity > 1
                )
                SELECT u.telegram_id, u.username, u.first_name, u.last_name,
                    mine.id AS receive_card_id, mine.name AS receive_card_name, mine.icon AS receive_card_icon,
                    theirs.id AS give_card_id, theirs.name AS give_card_name, theirs.icon AS give_card_icon,
                    LEAST(my_offer.quantity, 999) AS my_available,
                    LEAST(other_offer.quantity, 999) AS their_available
                FROM me
                JOIN my_offers my_offer ON TRUE
                JOIN other_wants ow ON ow.card_id = my_offer.card_id
                JOIN users u ON u.id = ow.user_id
                JOIN cards mine ON mine.id = my_offer.card_id
                JOIN other_offers other_offer ON other_offer.user_id = u.id
                JOIN my_wants mw ON mw.card_id = other_offer.card_id
                JOIN cards theirs ON theirs.id = other_offer.card_id
                ORDER BY u.username, u.first_name, u.telegram_id, mine.id, theirs.id
                """,
                (telegram_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    return _group_trade_matches(rows, limit)


def _get_trade_matches_sqlite(telegram_id: int, limit: int) -> list[dict]:
    # SQLite поддерживает CTE, запрос совместим с небольшими изменениями
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        rows = con.execute(
            """
            WITH me AS (SELECT id FROM users WHERE telegram_id = ?),
            my_wants AS (
                SELECT dc.card_id FROM desired_cards dc JOIN me ON me.id = dc.user_id
                LEFT JOIN user_cards uc ON uc.user_id = dc.user_id AND uc.card_id = dc.card_id
                WHERE COALESCE(uc.quantity, 0) = 0
            ),
            my_offers AS (
                SELECT uc.card_id, MAX(uc.quantity - 1, 0) AS quantity
                FROM user_cards uc JOIN me ON me.id = uc.user_id WHERE uc.quantity > 1
            ),
            other_wants AS (
                SELECT dc.user_id, dc.card_id FROM desired_cards dc JOIN me ON dc.user_id <> me.id
                LEFT JOIN user_cards uc ON uc.user_id = dc.user_id AND uc.card_id = dc.card_id
                WHERE COALESCE(uc.quantity, 0) = 0
            ),
            other_offers AS (
                SELECT uc.user_id, uc.card_id, MAX(uc.quantity - 1, 0) AS quantity
                FROM user_cards uc JOIN me ON uc.user_id <> me.id WHERE uc.quantity > 1
            )
            SELECT u.telegram_id, u.username, u.first_name, u.last_name,
                mine.id AS receive_card_id, mine.name AS receive_card_name, mine.icon AS receive_card_icon,
                theirs.id AS give_card_id, theirs.name AS give_card_name, theirs.icon AS give_card_icon,
                MIN(my_offer.quantity, 999) AS my_available,
                MIN(other_offer.quantity, 999) AS their_available
            FROM me
            JOIN my_offers my_offer
            JOIN other_wants ow ON ow.card_id = my_offer.card_id
            JOIN users u ON u.id = ow.user_id
            JOIN cards mine ON mine.id = my_offer.card_id
            JOIN other_offers other_offer ON other_offer.user_id = u.id
            JOIN my_wants mw ON mw.card_id = other_offer.card_id
            JOIN cards theirs ON theirs.id = other_offer.card_id
            ORDER BY u.username, u.first_name, u.telegram_id, mine.id, theirs.id
            """,
            (telegram_id,),
        ).fetchall()
    return _group_trade_matches(rows_to_dicts(rows), limit)


def _group_trade_matches(rows: list[dict], limit: int) -> list[dict]:
    grouped: dict[int, dict] = {}
    for row in rows:
        other_id = int(row["telegram_id"])
        match = grouped.setdefault(other_id, {
            "telegram_id": other_id, "username": row.get("username") or "",
            "first_name": row.get("first_name") or "", "last_name": row.get("last_name") or "",
            "trades": [],
        })
        trade = {
            "you_give": {"id": row["receive_card_id"], "name": row["receive_card_name"],
                         "icon": row["receive_card_icon"] or "", "available": int(row["my_available"])},
            "you_get": {"id": row["give_card_id"], "name": row["give_card_name"],
                        "icon": row["give_card_icon"] or "", "available": int(row["their_available"])},
        }
        if not any(
            x["you_give"]["id"] == trade["you_give"]["id"]
            and x["you_get"]["id"] == trade["you_get"]["id"]
            for x in match["trades"]
        ):
            match["trades"].append(trade)
    result = list(grouped.values())
    result.sort(key=lambda x: (-len(x["trades"]), (x["username"] or "").lower(), x["telegram_id"]))
    return result[:limit]
