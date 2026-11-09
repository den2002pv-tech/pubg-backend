"""
db_sqlite_schema.py — SQLite-совместимая схема и SQL-адаптеры.

Содержит:
  - DDL для создания всех таблиц в SQLite
  - Вспомогательные функции для перевода PG-специфичных конструкций
  - Утилиты для работы с SQLite dict-row (sqlite3.Row → dict)
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# DDL — SQLite-совместимые схемы
# ---------------------------------------------------------------------------

SQLITE_SCHEMA_SQL = """
-- Metadata proving that a PostgreSQL snapshot was completed before fallback.
CREATE TABLE IF NOT EXISTS database_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Основные пользователи
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    last_name  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen_at TEXT,
    last_activity_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_last_activity_at
    ON users (last_activity_at DESC);

-- Каталог карточек
CREATE TABLE IF NOT EXISTS cards (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    rarity INTEGER NOT NULL DEFAULT 1,
    icon   TEXT NOT NULL DEFAULT ''
);

-- Карточки пользователей
CREATE TABLE IF NOT EXISTS user_cards (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (user_id, card_id)
);

-- Желаемые карточки
CREATE TABLE IF NOT EXISTS desired_cards (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (user_id, card_id)
);

-- Обмены (устаревшая таблица, сохраняем для совместимости)
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Аналитика (properties хранится как JSON-строка вместо JSONB)
CREATE TABLE IF NOT EXISTS user_analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_user_analytics_created
    ON user_analytics_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_analytics_name_created
    ON user_analytics_events (event_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_analytics_user_created
    ON user_analytics_events (user_id, created_at DESC);

-- Заявки на обмен
CREATE TABLE IF NOT EXISTS trade_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    offered_card_id   TEXT NOT NULL,
    requested_card_id TEXT NOT NULL,
    offered_card_name   TEXT NOT NULL,
    offered_card_icon   TEXT NOT NULL DEFAULT '',
    requested_card_name TEXT NOT NULL,
    requested_card_icon TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','accepted','agreed','completed','declined','cancelled')),
    sender_agreed_at    TEXT,
    recipient_agreed_at TEXT,
    sender_completed_at    TEXT,
    recipient_completed_at TEXT,
    exchange_code_sent_at  TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (sender_user_id <> recipient_user_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_requests_sender_created
    ON trade_requests (sender_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_requests_recipient_created
    ON trade_requests (recipient_user_id, created_at DESC);

-- Дедупликация Telegram-webhook
CREATE TABLE IF NOT EXISTS processed_telegram_updates (
    update_id INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Outbox — журнал изменений для синхронизации с PG
CREATE TABLE IF NOT EXISTS outbox_operations (
    id            TEXT PRIMARY KEY,
    transaction_id TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    operation_type TEXT NOT NULL,
    payload       TEXT NOT NULL,
    sync_status   TEXT NOT NULL DEFAULT 'pending'
        CHECK (sync_status IN ('pending','synced','failed','superseded')),
    sync_attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_operations (sync_status, created_at);

-- Lock-таблица для защиты от dual-sync при нескольких workers
CREATE TABLE IF NOT EXISTS sync_lock (
    lock_key   TEXT PRIMARY KEY,
    holder_pid INTEGER,
    acquired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Таблица применённых операций (для idempotency при синхронизации)
CREATE TABLE IF NOT EXISTS outbox_applied_in_pg (
    op_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Cursor and delivery receipts for PostgreSQL's authoritative Outbox stream.
CREATE TABLE IF NOT EXISTS replicated_outbox_transactions (
    transaction_id TEXT PRIMARY KEY,
    transaction_sequence INTEGER NOT NULL UNIQUE,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

# Tables whose mutations are part of the PostgreSQL mirror. Trigger payloads
# contain full row images so deletes and updates can be replayed with conflict
# detection instead of being approximated as counter increments.
SYNC_TABLE_PRIMARY_KEYS = {
    "users": ("id",),
    "cards": ("id",),
    "user_cards": ("user_id", "card_id"),
    "desired_cards": ("user_id", "card_id"),
    "trades": ("id",),
    "user_analytics_events": ("id",),
    "trade_requests": ("id",),
    "processed_telegram_updates": ("update_id",),
}

# В PostgreSQL аналогичная таблица для idempotency
PG_OUTBOX_APPLIED_DDL = """
CREATE TABLE IF NOT EXISTS outbox_applied (
    op_id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def ensure_sqlite_schema(db_path: str) -> None:
    """Создать/обновить схему SQLite (идемпотентно)."""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    con.create_function("database_outbox_enabled", 0, lambda: 0)
    con.create_function("database_transaction_id", 0, lambda: str(uuid.uuid4()))
    con.executescript(SQLITE_SCHEMA_SQL)
    columns = {row[1] for row in con.execute("PRAGMA table_info(outbox_operations)")}
    if "transaction_id" not in columns:
        con.execute("ALTER TABLE outbox_operations ADD COLUMN transaction_id TEXT")
    install_outbox_triggers(con)
    con.commit()
    con.close()


def install_outbox_triggers(con: sqlite3.Connection) -> None:
    """Install transactional row-image outbox triggers for all mirrored tables."""
    for table, primary_key in SYNC_TABLE_PRIMARY_KEYS.items():
        columns = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            raise sqlite3.DatabaseError(f"Missing SQLite sync table: {table}")
        pk_json = lambda prefix: "json_object(" + ", ".join(
            f"'{key}', {prefix}.\"{key}\"" for key in primary_key
        ) + ")"
        row_json = lambda prefix: "json_object(" + ", ".join(
            f"'{column}', {prefix}.\"{column}\"" for column in columns
        ) + ")"
        for action, prefix in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
            trigger = f"trg_outbox_{table}_{action}"
            con.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            old_expr = "NULL" if action == "insert" else row_json("OLD")
            new_expr = "NULL" if action == "delete" else row_json("NEW")
            sql = f'''
                CREATE TRIGGER IF NOT EXISTS "{trigger}"
                AFTER {action.upper()} ON "{table}"
                WHEN database_outbox_enabled() = 1
                BEGIN
                    INSERT INTO outbox_operations
                        (id, transaction_id, operation_type, payload, sync_status)
                    VALUES (
                        lower(hex(randomblob(16))),
                        database_transaction_id(),
                        'sqlite_row_change',
                        json_object(
                            'table', '{table}',
                            'action', '{action}',
                            'pk', {pk_json(prefix)},
                            'before', {old_expr},
                            'after', {new_expr}
                        ),
                        'pending'
                    );
                END;
            '''
            con.execute(sql)


# ---------------------------------------------------------------------------
# Утилиты преобразования строк
# ---------------------------------------------------------------------------

def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Преобразует sqlite3.Row в обычный dict (рекурсивно)."""
    if row is None:
        return None
    result = dict(row)
    # Парсим JSON-строки в поле properties
    if "properties" in result and isinstance(result["properties"], str):
        try:
            result["properties"] = json.loads(result["properties"])
        except (json.JSONDecodeError, TypeError):
            result["properties"] = {}
    # Преобразуем timestamp-строки в datetime (для совместимости с psycopg)
    for key in ("created_at", "last_seen_at", "last_activity_at", "updated_at",
                "sender_agreed_at", "recipient_agreed_at", "sender_completed_at",
                "recipient_completed_at", "exchange_code_sent_at", "processed_at"):
        if key in result and isinstance(result[key], str) and result[key]:
            try:
                result[key] = _parse_sqlite_ts(result[key])
            except Exception:
                pass
    return result


def rows_to_dicts(rows: list) -> list[dict]:
    """Преобразует список sqlite3.Row в список dict."""
    return [row_to_dict(r) for r in rows if r is not None]


def _parse_sqlite_ts(ts_str: str) -> datetime:
    """Парсинг ISO timestamp из SQLite в timezone-aware datetime."""
    ts_str = ts_str.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            dt = datetime.strptime(ts_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Последняя попытка через fromisoformat
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def now_sqlite() -> str:
    """Текущий UTC timestamp в формате SQLite."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Адаптеры SQL-запросов: PG → SQLite
# ---------------------------------------------------------------------------

def make_placeholders(count: int) -> str:
    """Создать строку плейсхолдеров для SQLite: ?,?,?"""
    return ",".join("?" * count)


def card_id_order_sql_sqlite() -> str:
    """SQLite-совместимый ORDER BY для сортировки card_N числово."""
    return """
    ORDER BY
        CASE
            WHEN c.id GLOB 'card_[0-9]*'
                THEN CAST(SUBSTR(c.id, 6) AS INTEGER)
            ELSE 2147483647
        END,
        c.id
    """


def desired_cards_order_sql_sqlite() -> str:
    """То же, что card_id_order_sql_sqlite, но для desired_cards."""
    return """
    ORDER BY
        CASE
            WHEN c.id GLOB 'card_[0-9]*'
                THEN CAST(SUBSTR(c.id, 6) AS INTEGER)
            ELSE 2147483647
        END,
        c.id
    """


# ---------------------------------------------------------------------------
# Регулярное выражение: SQLite не поддерживает ~ напрямую.
# Регистрируем Python-функцию regexp() через con.create_function.
# ---------------------------------------------------------------------------

def _regexp(pattern: str, value: str | None) -> bool:
    if value is None:
        return False
    return bool(re.search(pattern, str(value)))


def setup_sqlite_functions(con: sqlite3.Connection, outbox_enabled: bool = True, transaction_id: str | None = None) -> None:
    """Зарегистрировать пользовательские функции SQLite."""
    con.create_function("regexp", 2, _regexp)
    con.create_function("now_utc", 0, now_sqlite)
    con.create_function("database_outbox_enabled", 0, lambda: int(outbox_enabled))
    current_transaction_id = {"value": transaction_id}

    def transaction_id_for_current_transaction() -> str:
        if current_transaction_id["value"] is None:
            current_transaction_id["value"] = str(uuid.uuid4())
        return current_transaction_id["value"]

    def reset_transaction_id(statement: str) -> None:
        command = statement.strip().split(None, 1)[0].upper() if statement.strip() else ""
        if command in {"COMMIT", "END"} or statement.strip().upper() == "ROLLBACK":
            current_transaction_id["value"] = None
        elif command == "BEGIN":
            current_transaction_id["value"] = str(uuid.uuid4())

    con.create_function("database_transaction_id", 0, transaction_id_for_current_transaction)
    con.set_trace_callback(reset_transaction_id)


def adapt_sqlite_query(query: str) -> tuple[str, bool]:
    """Translate only known DB-API/PostgreSQL syntax used by this project.

    The scanner keeps quoted strings and comments intact. FOR UPDATE is handled
    by taking SQLite's write reservation before the SELECT, preserving the
    intended row-lock guarantee at SQLite's database-wide writer granularity.
    """
    import re

    sql = []
    i = 0
    quote = None
    line_comment = False
    block_comment = False
    lock_requested = False
    while i < len(query):
        char = query[i]
        following = query[i + 1] if i + 1 < len(query) else ""
        if line_comment:
            sql.append(char)
            if char == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            sql.append(char)
            if char == "*" and following == "/":
                sql.append(following)
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            sql.append(char)
            if char == quote:
                if following == quote:
                    sql.append(following)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char == "-" and following == "-":
            sql.extend((char, following))
            line_comment = True
            i += 2
            continue
        if char == "/" and following == "*":
            sql.extend((char, following))
            block_comment = True
            i += 2
            continue
        if char in "'\"`":
            quote = char
            sql.append(char)
            i += 1
            continue
        if query[i:i + 2] == "%s":
            sql.append("?")
            i += 2
            continue
        if query[i:i + 5].upper() == "NOW()":
            sql.append("now_utc()")
            i += 5
            continue
        if query[i:i + 7].lower() == "::jsonb":
            i += 7
            continue
        if query[i:i + 10].upper() == "FOR UPDATE" and (i == 0 or not query[i - 1].isalnum()):
            match = re.match(r"FOR\s+UPDATE(?:\s+OF\s+[A-Za-z_][A-Za-z0-9_]*)?", query[i:], re.I)
            if match:
                lock_requested = True
                i += match.end()
                continue
        sql.append(char)
        i += 1

    return "".join(sql), lock_requested


# ---------------------------------------------------------------------------
# Утилита: список в IN-плейсхолдеры для SQLite
# ---------------------------------------------------------------------------

def in_placeholders(items: list) -> tuple[str, list]:
    """
    Преобразует список в (placeholders_str, params_list) для SQLite IN-clause.
    Пример: in_placeholders(['a','b']) → ('?,?', ['a','b'])
    """
    if not items:
        return "NULL", []  # IN (NULL) всегда False
    return make_placeholders(len(items)), list(items)


# ---------------------------------------------------------------------------
# JSONB-совместимость для analytics в SQLite
# ---------------------------------------------------------------------------

def extract_json_field(properties_str: str | dict, field: str) -> Any:
    """Аналог PostgreSQL properties->>'field'."""
    if isinstance(properties_str, dict):
        return properties_str.get(field)
    if not isinstance(properties_str, str):
        return None
    try:
        data = json.loads(properties_str)
        return data.get(field)
    except (json.JSONDecodeError, TypeError):
        return None


def has_json_field(properties_str: str | dict, field: str) -> bool:
    """Аналог PostgreSQL properties ? 'field'."""
    return extract_json_field(properties_str, field) is not None


def remove_json_fields(properties_str: str | dict, *fields: str) -> str:
    """Аналог PostgreSQL properties - 'key1' - 'key2'."""
    if isinstance(properties_str, dict):
        data = properties_str
    else:
        try:
            data = json.loads(properties_str or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    for f in fields:
        data.pop(f, None)
    return json.dumps(data, ensure_ascii=False)


def parse_properties(properties_str: str | dict | None) -> dict:
    """Безопасно распарсить JSON-свойства."""
    if isinstance(properties_str, dict):
        return properties_str
    if not properties_str:
        return {}
    try:
        return json.loads(properties_str)
    except (json.JSONDecodeError, TypeError):
        return {}
