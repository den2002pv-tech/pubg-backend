<<<<<<< LOCAL (Your changes)
<<<<<<< LOCAL (Your changes)
"""SQLite-primary data access with an optional PostgreSQL snapshot target."""
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

from database_manager import get_manager
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

CARD_CATALOG_ALLOW_DELETE_ENV = "CARD_CATALOG_ALLOW_DELETE"
CARD_CATALOG_MAX_SHRINK_PERCENT_ENV = "CARD_CATALOG_MAX_SHRINK_PERCENT"


def _catalogue_allow_delete() -> bool:
    value = os.getenv(CARD_CATALOG_ALLOW_DELETE_ENV, "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _catalogue_max_shrink_percent() -> float:
    raw = os.getenv(CARD_CATALOG_MAX_SHRINK_PERCENT_ENV, "10").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{CARD_CATALOG_MAX_SHRINK_PERCENT_ENV} must be a number") from exc
    if not 0 <= value <= 100:
        raise ValueError(f"{CARD_CATALOG_MAX_SHRINK_PERCENT_ENV} must be 0..100")
    return value


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
    """Yield a local SQLite connection; PostgreSQL is never in the request path."""
    with _sqlite_connection_ctx() as conn:
        yield conn


@contextmanager
def _sqlite_connection_ctx():
    """Контекст SQLite-соединения с адаптером."""
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        setup_sqlite_functions(con)
        yield _SQLiteConnectionAdapter(con, mgr.mark_dirty)


# ---------------------------------------------------------------------------
# Обёртки для совместимости интерфейса psycopg / sqlite3
# ---------------------------------------------------------------------------

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

    def __init__(self, con: sqlite3.Connection, on_commit=None) -> None:
        self._con = con
        self._cursor_obj = None
        self._on_commit = on_commit
        self._changes_before_transaction = con.total_changes

    def cursor(self):
        raw = self._con.cursor()
        self._cursor_obj = _SQLiteCursor(raw, self._con)
        return self._cursor_obj

    def commit(self):
        self._con.commit()
        if self._con.total_changes > self._changes_before_transaction and self._on_commit:
            self._on_commit()
        self._changes_before_transaction = self._con.total_changes

    def rollback(self):
        self._con.rollback()
        self._changes_before_transaction = self._con.total_changes

    def execute(self, query: str, params=None):
        raw = self._con.cursor()
        cur = _SQLiteCursor(raw, self._con)
        cur.execute(query, params)
        return cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        return False


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the local SQLite schema idempotently."""
    from database_manager import _sqlite_path
    from db_sqlite_schema import ensure_sqlite_schema
    ensure_sqlite_schema(_sqlite_path())




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
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (telegram_id, username, first_name, last_name, last_seen_at, last_activity_at)
                   VALUES (?, ?, ?, ?, now_utc(), now_utc())
                   ON CONFLICT (telegram_id) DO UPDATE SET
                     username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE users.username END,
                     first_name=CASE WHEN excluded.first_name<>'' THEN excluded.first_name ELSE users.first_name END,
                     last_name=CASE WHEN excluded.last_name<>'' THEN excluded.last_name ELSE users.last_name END,
                     last_seen_at=now_utc(), last_activity_at=now_utc()""",
                (payload["telegram_id"], payload["username"], payload["first_name"], payload["last_name"]),
            )
            cur.execute("SELECT id, telegram_id, username, first_name, last_name, created_at, last_seen_at, last_activity_at FROM users WHERE telegram_id=?", (payload["telegram_id"],))
            user = cur.fetchone()
        conn.commit()
    return dict(user)






# ---------------------------------------------------------------------------
# get_admin_users
# ---------------------------------------------------------------------------

def get_admin_users(search: str = "", online_seconds: int = 90, limit: int = 50, offset: int = 0) -> dict:
    search = (search or "").strip()[:200]
    online_seconds = max(30, min(int(online_seconds), 600))
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    return _get_admin_users_sqlite(search, online_seconds, limit, offset)




def _get_admin_users_sqlite(search: str, online_seconds: int, limit: int, offset: int) -> dict:
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=online_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pattern = f"%{search}%"
    with get_connection() as con:
        # Фильтр поиска
        if search:
            where = "WHERE (CAST(u.telegram_id AS TEXT) LIKE ? OR u.username LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ?)"
            search_params = [pattern, pattern, pattern, pattern]
        else:
            where = ""
            search_params = []

        cur = con.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS total, SUM(CASE WHEN u.last_activity_at >= ? THEN 1 ELSE 0 END) AS online FROM users u {where}",
            [cutoff] + search_params,
        )
        summary = cur.fetchone()
        total = int(summary["total"]) if summary else 0
        online = int(summary["online"] or 0) if summary else 0

        order_null = "CASE WHEN u.last_activity_at IS NULL THEN 1 ELSE 0 END"
        cur.execute(
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
        )
        users = rows_to_dicts(cur.fetchall())
    return {"users": users, "total": total, "online": online,
            "limit": limit, "offset": offset, "online_seconds": online_seconds}


# ---------------------------------------------------------------------------
# get_admin_user
# ---------------------------------------------------------------------------

def get_admin_user(telegram_id: int) -> dict | None:
    return _get_admin_user_sqlite(telegram_id)




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
            if get_manager().is_postgres:
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
            else:
                for card_id, quantity in quantities.items():
                    cur.execute(
                        """
                        INSERT INTO user_cards (user_id, card_id, quantity)
                        SELECT id, ?, ? FROM users WHERE telegram_id = ?
                        ON CONFLICT (user_id, card_id) DO UPDATE SET quantity = excluded.quantity
                        """,
                        (card_id, quantity, telegram_id),
                    )
            if owned_card_ids:
                if get_manager().is_postgres:
                    cur.execute(
                        """DELETE FROM desired_cards
                           WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                             AND card_id = ANY(%s)""",
                        (telegram_id, owned_card_ids),
                    )
                else:
                    placeholders = ", ".join("?" for _ in owned_card_ids)
                    cur.execute(
                        f"""DELETE FROM desired_cards
                            WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
                              AND card_id IN ({placeholders})""",
                        (telegram_id, *owned_card_ids),
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
    return _get_desired_cards_sqlite(telegram_id)




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
    return _set_desired_cards_sqlite(payload)




def _set_desired_cards_sqlite(payload: dict) -> list[dict]:
    telegram_id, card_ids = payload["telegram_id"], payload["card_ids"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM desired_cards WHERE user_id=(SELECT id FROM users WHERE telegram_id=?)", (telegram_id,))
            if card_ids:
                placeholders = ",".join("?" for _ in card_ids)
                cur.execute(
                    f"""INSERT INTO desired_cards(user_id, card_id)
                        SELECT u.id,c.id FROM users u JOIN cards c ON c.id IN ({placeholders})
                        LEFT JOIN user_cards uc ON uc.user_id=u.id AND uc.card_id=c.id
                        WHERE u.telegram_id=? AND COALESCE(uc.quantity,0)=0 ON CONFLICT DO NOTHING""",
                    (*card_ids, telegram_id),
                )
        conn.commit()
    return get_desired_cards(telegram_id)


# ---------------------------------------------------------------------------
# get_user_cards
# ---------------------------------------------------------------------------

def get_user_cards(telegram_id: int) -> list[dict]:
    return _get_user_cards_sqlite(telegram_id)




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
    return _add_zero_cards_sqlite(payload)




def _add_zero_cards_sqlite(payload: dict) -> int:
    telegram_id, card_ids = payload["telegram_id"], payload["card_ids"]
    placeholders = ",".join("?" for _ in card_ids)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO user_cards(user_id,card_id,quantity)
                    SELECT u.id,c.id,0 FROM users u JOIN cards c ON c.id IN ({placeholders})
                    WHERE u.telegram_id=? ON CONFLICT(user_id,card_id) DO NOTHING""",
                (*card_ids, telegram_id),
            )
            added = cur.rowcount
        conn.commit()
    return added


def _sync_cards_sqlite(valid_cards, active_ids, catalogue_is_complete: bool) -> int:
    count = 0
    delete_allowed = _catalogue_allow_delete()
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in valid_cards:
                try:
                    rarity = int(info.get("rarity", 1))
                except (TypeError, ValueError):
                    rarity = 1
                cur.execute(
                    """INSERT INTO cards(id,name,rarity,icon) VALUES(?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,rarity=excluded.rarity,icon=excluded.icon""",
                    (card_id, str(info.get("name") or card_id), rarity, str(info.get("icon") or "")),
                )
                count += 1
            if catalogue_is_complete and active_ids and delete_allowed:
                cur.execute("SELECT COUNT(*) AS total FROM cards")
                row = cur.fetchone()
                existing = int(row["total"]) if row else 0
                new_count = len(active_ids)
                if existing > 0:
                    shrink = (existing - new_count) * 100.0 / existing
                    max_shrink = _catalogue_max_shrink_percent()
                    if shrink > max_shrink:
                        raise RuntimeError(
                            f"Card catalogue shrank from {existing} to {new_count} "
                            f"({shrink:.1f}% > {max_shrink}%). Refusing to delete existing cards. "
                            f"Set {CARD_CATALOG_ALLOW_DELETE_ENV}=1 and explicitly raise "
                            f"{CARD_CATALOG_MAX_SHRINK_PERCENT_ENV}."
                        )
                placeholders = ",".join("?" for _ in active_ids)
                cur.execute(f"DELETE FROM cards WHERE id NOT IN ({placeholders})", active_ids)
        conn.commit()
    return count


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

    return _sync_cards_sqlite(valid_cards, active_ids, catalogue_is_complete)




# ---------------------------------------------------------------------------
# get_trade_matches
# ---------------------------------------------------------------------------

def get_trade_matches(telegram_id: int, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    return _get_trade_matches_sqlite(telegram_id, limit)




def _get_trade_matches_sqlite(telegram_id: int, limit: int) -> list[dict]:
    # SQLite поддерживает CTE, запрос совместим с небольшими изменениями
    with get_connection() as con:
        with con.cursor() as cur:
            cur.execute(
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
            )
            rows = cur.fetchall()
    return _group_trade_matches(rows, limit)


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

=======
from __future__ import annotations

import os
import time
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


_DB_METRICS: ContextVar[dict | None] = ContextVar("db_metrics", default=None)


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


class _TimedCursor(psycopg.Cursor):
    def execute(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return super().execute(*args, **kwargs)
        finally:
            _add_metric("db_sql_ms", (time.perf_counter() - started) * 1000)
            _add_metric("db_sql_count", 1)


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    started = time.perf_counter()
    try:
        conn = psycopg.connect(_database_url(), row_factory=dict_row,
                               cursor_factory=_TimedCursor)
    finally:
        _add_metric("db_connection_ms", (time.perf_counter() - started) * 1000)
    _add_metric("db_connection_count", 1)
    transaction_started = time.perf_counter()
    try:
        with conn:
            yield conn
    finally:
        # Includes SQL, commit/rollback and connection context cleanup; nested in API time.
        _add_metric("db_transaction_ms", (time.perf_counter() - transaction_started) * 1000)


def init_db() -> None:
    """Create the application tables if they do not exist yet."""
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


def get_or_create_user(
    telegram_id: int,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
) -> dict:
    """Create a user and refresh their public Telegram profile when available."""
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
                (telegram_id, username.strip().lstrip("@"), first_name.strip(), last_name.strip()),
            )
            user = cur.fetchone()
        conn.commit()
    return dict(user)


def get_admin_users(search: str = "", online_seconds: int = 90, limit: int = 50, offset: int = 0) -> dict:
    """Return a paginated, searchable user directory with activity and collection stats."""
    search = (search or "").strip()[:200]
    online_seconds = max(30, min(int(online_seconds), 600))
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
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


def get_admin_user(telegram_id: int) -> dict | None:
    """Return one user and aggregate collection stats without join multiplication."""
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


def set_user_card_quantity(telegram_id: int, card_id: str, quantity: int) -> None:
    quantity = max(0, int(quantity))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_cards (user_id, card_id, quantity)
                SELECT id, %s, %s FROM users WHERE telegram_id = %s
                ON CONFLICT (user_id, card_id) DO UPDATE SET quantity = EXCLUDED.quantity
                """,
                (card_id, quantity, telegram_id),
            )
            if quantity > 0:
                cur.execute(
                    """
                    DELETE FROM desired_cards
                    WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                      AND card_id = %s
                    """,
                    (telegram_id, card_id),
                )
        conn.commit()


def set_user_card_quantities(telegram_id: int, updates: list[tuple[str, int]]) -> None:
    """Atomically persist absolute card quantities and remove newly owned cards from the wishlist."""
    quantities: dict[str, int] = {}
    for card_id, quantity in updates:
        normalized_id = str(card_id).strip()
        if normalized_id:
            quantities[normalized_id] = max(quantities.get(normalized_id, 0), max(0, int(quantity)))
    if not quantities:
        return

    card_ids = list(quantities)
    card_quantities = [quantities[card_id] for card_id in card_ids]
    owned_card_ids = [
        card_id for card_id, quantity in quantities.items()
        if quantity > 0
    ]

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
                    """
                    DELETE FROM desired_cards
                    WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                      AND card_id = ANY(%s)
                    """,
                    (telegram_id, owned_card_ids),
                )
        conn.commit()


def clear_user_cards(telegram_id: int) -> int:
    """Set all inventory quantities to zero without deleting known cards."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_cards
                   SET quantity = 0
                 WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                   AND quantity <> 0
                """,
                (telegram_id,),
            )
            changed = cur.rowcount
        conn.commit()
    return changed


def get_desired_cards(telegram_id: int) -> list[dict]:
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


def set_desired_cards(telegram_id: int, card_ids: list[str]) -> list[dict]:
    unique_ids = list(dict.fromkeys(str(x).strip() for x in card_ids if str(x).strip()))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM desired_cards
                WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                """,
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


def get_user_cards(telegram_id: int) -> list[dict]:
    """Return every master card with the user's quantity; missing rows are 0."""
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


def add_zero_cards(telegram_id: int, card_ids: list[str]) -> int:
    """Persist selected known cards with quantity zero."""
    unique_ids = list(dict.fromkeys(str(card_id) for card_id in card_ids if card_id))
    if not unique_ids:
        return 0
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


def sync_cards(cards: dict) -> int:
    """Synchronize PostgreSQL's master catalogue with card_hashes.json.

    A non-empty, valid catalogue is authoritative: cards removed from the JSON
    are removed from PostgreSQL too. Foreign keys cascade their obsolete
    per-user inventory and wishlist rows. An empty/invalid catalogue is treated
    as unavailable and never triggers mass deletion.
    """
    valid_cards = [
        (str(card_id), info)
        for card_id, info in cards.items()
        if isinstance(info, dict)
    ]
    if not valid_cards:
        return 0
    # A partially malformed catalogue is not authoritative for deletions:
    # removing even one invalid entry here would cascade-delete that card's
    # user inventory and wishlist rows from PostgreSQL during startup.
    catalogue_is_complete = len(valid_cards) == len(cards)

    count = 0
    active_ids = [card_id for card_id, _info in valid_cards]
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

            if catalogue_is_complete:
                cur.execute(
                    "DELETE FROM cards WHERE NOT (id = ANY(%s))",
                    (active_ids,),
                )
        conn.commit()
    return count



def get_trade_matches(telegram_id: int, limit: int = 50) -> list[dict]:
    """Find users with at least one reciprocal card trade; first copy is reserved."""
    limit = max(1, min(int(limit), 100))
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
    grouped: dict[int, dict] = {}
    for row in rows:
        other_id = int(row["telegram_id"])
        match = grouped.setdefault(other_id, {
            "telegram_id": other_id, "username": row.get("username") or "",
            "first_name": row.get("first_name") or "", "last_name": row.get("last_name") or "", "trades": [],
        })
        trade = {
            "you_give": {"id": row["receive_card_id"], "name": row["receive_card_name"],
                         "icon": row["receive_card_icon"] or "", "available": int(row["my_available"])},
            "you_get": {"id": row["give_card_id"], "name": row["give_card_name"],
                        "icon": row["give_card_icon"] or "", "available": int(row["their_available"])},
        }
        if not any(x["you_give"]["id"] == trade["you_give"]["id"] and x["you_get"]["id"] == trade["you_get"]["id"] for x in match["trades"]):
            match["trades"].append(trade)
    result = list(grouped.values())
    result.sort(key=lambda x: (-len(x["trades"]), (x["username"] or "").lower(), x["telegram_id"]))
    return result[:limit]

>>>>>>> REMOTE (Changes from GitHub)

=======
"""SQLite-primary data access with an optional PostgreSQL snapshot target."""
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

from database_manager import get_manager
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
    """Yield a local SQLite connection; PostgreSQL is never in the request path."""
    with _sqlite_connection_ctx() as conn:
        yield conn


@contextmanager
def _sqlite_connection_ctx():
    """Контекст SQLite-соединения с адаптером."""
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        setup_sqlite_functions(con)
        yield _SQLiteConnectionAdapter(con, mgr.mark_dirty)


# ---------------------------------------------------------------------------
# Обёртки для совместимости интерфейса psycopg / sqlite3
# ---------------------------------------------------------------------------

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

    def __init__(self, con: sqlite3.Connection, on_commit=None) -> None:
        self._con = con
        self._cursor_obj = None
        self._on_commit = on_commit
        self._changes_before_transaction = con.total_changes

    def cursor(self):
        raw = self._con.cursor()
        self._cursor_obj = _SQLiteCursor(raw, self._con)
        return self._cursor_obj

    def commit(self):
        self._con.commit()
        if self._con.total_changes > self._changes_before_transaction and self._on_commit:
            self._on_commit()
        self._changes_before_transaction = self._con.total_changes

    def rollback(self):
        self._con.rollback()
        self._changes_before_transaction = self._con.total_changes

    def execute(self, query: str, params=None):
        raw = self._con.cursor()
        cur = _SQLiteCursor(raw, self._con)
        cur.execute(query, params)
        return cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        return False


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the local SQLite schema idempotently."""
    from database_manager import _sqlite_path
    from db_sqlite_schema import ensure_sqlite_schema
    ensure_sqlite_schema(_sqlite_path())


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
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (telegram_id, username, first_name, last_name, last_seen_at, last_activity_at)
                   VALUES (?, ?, ?, ?, now_utc(), now_utc())
                   ON CONFLICT (telegram_id) DO UPDATE SET
                     username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE users.username END,
                     first_name=CASE WHEN excluded.first_name<>'' THEN excluded.first_name ELSE users.first_name END,
                     last_name=CASE WHEN excluded.last_name<>'' THEN excluded.last_name ELSE users.last_name END,
                     last_seen_at=now_utc(), last_activity_at=now_utc()""",
                (payload["telegram_id"], payload["username"], payload["first_name"], payload["last_name"]),
            )
            cur.execute("SELECT id, telegram_id, username, first_name, last_name, created_at, last_seen_at, last_activity_at FROM users WHERE telegram_id=?", (payload["telegram_id"],))
            user = cur.fetchone()
        conn.commit()
    return dict(user)


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


# ---------------------------------------------------------------------------
# get_admin_users
# ---------------------------------------------------------------------------

def get_admin_users(search: str = "", online_seconds: int = 90, limit: int = 50, offset: int = 0) -> dict:
    search = (search or "").strip()[:200]
    online_seconds = max(30, min(int(online_seconds), 600))
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
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
    with get_connection() as con:
        # Фильтр поиска
        if search:
            where = "WHERE (CAST(u.telegram_id AS TEXT) LIKE ? OR u.username LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ?)"
            search_params = [pattern, pattern, pattern, pattern]
        else:
            where = ""
            search_params = []

        cur = con.cursor()
        cur.execute(
            f"SELECT COUNT(*) AS total, SUM(CASE WHEN u.last_activity_at >= ? THEN 1 ELSE 0 END) AS online FROM users u {where}",
            [cutoff] + search_params,
        )
        summary = cur.fetchone()
        total = int(summary["total"]) if summary else 0
        online = int(summary["online"] or 0) if summary else 0

        order_null = "CASE WHEN u.last_activity_at IS NULL THEN 1 ELSE 0 END"
        cur.execute(
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
        )
        users = cur.fetchall()
    return {"users": users, "total": total, "online": online,
            "limit": limit, "offset": offset, "online_seconds": online_seconds}


# ---------------------------------------------------------------------------
# get_admin_user
# ---------------------------------------------------------------------------

def get_admin_user(telegram_id: int) -> dict | None:
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
            if get_manager().is_postgres:
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
            else:
                for card_id, quantity in quantities.items():
                    cur.execute(
                        """
                        INSERT INTO user_cards (user_id, card_id, quantity)
                        SELECT id, ?, ? FROM users WHERE telegram_id = ?
                        ON CONFLICT (user_id, card_id) DO UPDATE SET quantity = excluded.quantity
                        """,
                        (card_id, quantity, telegram_id),
                    )
            if owned_card_ids:
                if get_manager().is_postgres:
                    cur.execute(
                        """DELETE FROM desired_cards
                           WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                             AND card_id = ANY(%s)""",
                        (telegram_id, owned_card_ids),
                    )
                else:
                    placeholders = ", ".join("?" for _ in owned_card_ids)
                    cur.execute(
                        f"""DELETE FROM desired_cards
                            WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
                              AND card_id IN ({placeholders})""",
                        (telegram_id, *owned_card_ids),
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
    return _set_desired_cards_sqlite(payload)


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


def _set_desired_cards_sqlite(payload: dict) -> list[dict]:
    telegram_id, card_ids = payload["telegram_id"], payload["card_ids"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM desired_cards WHERE user_id=(SELECT id FROM users WHERE telegram_id=?)", (telegram_id,))
            if card_ids:
                placeholders = ",".join("?" for _ in card_ids)
                cur.execute(
                    f"""INSERT INTO desired_cards(user_id, card_id)
                        SELECT u.id,c.id FROM users u JOIN cards c ON c.id IN ({placeholders})
                        LEFT JOIN user_cards uc ON uc.user_id=u.id AND uc.card_id=c.id
                        WHERE u.telegram_id=? AND COALESCE(uc.quantity,0)=0 ON CONFLICT DO NOTHING""",
                    (*card_ids, telegram_id),
                )
        conn.commit()
    return get_desired_cards(telegram_id)


# ---------------------------------------------------------------------------
# get_user_cards
# ---------------------------------------------------------------------------

def get_user_cards(telegram_id: int) -> list[dict]:
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
    return _add_zero_cards_sqlite(payload)


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


def _add_zero_cards_sqlite(payload: dict) -> int:
    telegram_id, card_ids = payload["telegram_id"], payload["card_ids"]
    placeholders = ",".join("?" for _ in card_ids)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO user_cards(user_id,card_id,quantity)
                    SELECT u.id,c.id,0 FROM users u JOIN cards c ON c.id IN ({placeholders})
                    WHERE u.telegram_id=? ON CONFLICT(user_id,card_id) DO NOTHING""",
                (*card_ids, telegram_id),
            )
            added = cur.rowcount
        conn.commit()
    return added


def _sync_cards_sqlite(valid_cards, active_ids, catalogue_is_complete: bool) -> int:
    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in valid_cards:
                try:
                    rarity = int(info.get("rarity", 1))
                except (TypeError, ValueError):
                    rarity = 1
                cur.execute(
                    """INSERT INTO cards(id,name,rarity,icon) VALUES(?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,rarity=excluded.rarity,icon=excluded.icon""",
                    (card_id, str(info.get("name") or card_id), rarity, str(info.get("icon") or "")),
                )
                count += 1
            if catalogue_is_complete and active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                cur.execute(f"DELETE FROM cards WHERE id NOT IN ({placeholders})", active_ids)
        conn.commit()
    return count


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

    return _sync_cards_sqlite(valid_cards, active_ids, catalogue_is_complete)


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
    with get_connection() as con:
        with con.cursor() as cur:
            cur.execute(
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
            )
            rows = cur.fetchall()
    return _group_trade_matches(rows, limit)


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

>>>>>>> REMOTE (Changes from GitHub)
