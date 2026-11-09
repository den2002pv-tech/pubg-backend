"""Transactional PostgreSQL Outbox and ordered SQLite projection consumer."""
from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)
_sqlite_schema_lock = threading.Lock()
_sqlite_schema_paths: set[str] = set()

# Операции, которые являются абсолютными (последняя вытесняет предыдущие для той же пары)
SUPERSEDEABLE_OPS = {
    "set_user_card_quantity",    # (telegram_id, card_id) → quantity
    "set_user_card_quantities",  # (telegram_id,) → все карточки
    "clear_user_cards",          # (telegram_id,) → все карточки в 0
    "set_desired_cards",         # (telegram_id,) → весь список желаемых
}


class RowChangeConflict(RuntimeError):
    """PostgreSQL diverged from both the recorded base and local row image."""


class OutboxGapError(RuntimeError):
    """A committed PostgreSQL Outbox transaction or one of its events is missing."""


def ensure_postgres_outbox_schema(pg_conn, install_tables: bool = True) -> None:
    """Install the PG-side transactional journal and row-image triggers.

    A transaction-scoped advisory lock serializes transactions that modify
    mirrored tables. Its transactional clock has no sequence holes on rollback,
    so the SQLite consumer can distinguish an aborted operation from lost data.
    """
    from db_sqlite_schema import SYNC_TABLE_PRIMARY_KEYS

    pg_conn.execute("""
        CREATE TABLE IF NOT EXISTS app_outbox_clock (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            last_sequence BIGINT NOT NULL DEFAULT 0
        )
    """)
    pg_conn.execute("""
        INSERT INTO app_outbox_clock (singleton, last_sequence)
        VALUES (TRUE, 0) ON CONFLICT (singleton) DO NOTHING
    """)
    pg_conn.execute("""
        CREATE TABLE IF NOT EXISTS app_outbox_transactions (
            transaction_id TEXT PRIMARY KEY,
            transaction_sequence BIGINT NOT NULL UNIQUE,
            event_count INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    pg_conn.execute("""
        CREATE TABLE IF NOT EXISTS app_outbox_events (
            transaction_id TEXT NOT NULL REFERENCES app_outbox_transactions(transaction_id) ON DELETE CASCADE,
            event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
            table_name TEXT NOT NULL,
            operation TEXT NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
            row_key JSONB NOT NULL,
            before_row JSONB,
            after_row JSONB,
            PRIMARY KEY (transaction_id, event_sequence)
        )
    """)
    pg_conn.execute("""
        CREATE OR REPLACE FUNCTION capture_app_outbox_row_change()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_transaction_id TEXT := txid_current()::TEXT;
            v_transaction_sequence BIGINT;
            v_event_sequence INTEGER;
            v_before JSONB;
            v_after JSONB;
            v_key JSONB;
        BEGIN
            PERFORM pg_advisory_xact_lock(716250919481);

            IF TG_OP <> 'INSERT' THEN
                v_before := to_jsonb(OLD);
            END IF;
            IF TG_OP <> 'DELETE' THEN
                v_after := to_jsonb(NEW);
            END IF;

            v_key := CASE TG_TABLE_NAME
                WHEN 'users' THEN jsonb_build_object('id', COALESCE(v_after->'id', v_before->'id'))
                WHEN 'cards' THEN jsonb_build_object('id', COALESCE(v_after->'id', v_before->'id'))
                WHEN 'user_cards' THEN jsonb_build_object(
                    'user_id', COALESCE(v_after->'user_id', v_before->'user_id'),
                    'card_id', COALESCE(v_after->'card_id', v_before->'card_id'))
                WHEN 'desired_cards' THEN jsonb_build_object(
                    'user_id', COALESCE(v_after->'user_id', v_before->'user_id'),
                    'card_id', COALESCE(v_after->'card_id', v_before->'card_id'))
                WHEN 'trades' THEN jsonb_build_object('id', COALESCE(v_after->'id', v_before->'id'))
                WHEN 'user_analytics_events' THEN jsonb_build_object('id', COALESCE(v_after->'id', v_before->'id'))
                WHEN 'trade_requests' THEN jsonb_build_object('id', COALESCE(v_after->'id', v_before->'id'))
                WHEN 'processed_telegram_updates' THEN jsonb_build_object(
                    'update_id', COALESCE(v_after->'update_id', v_before->'update_id'))
                ELSE NULL
            END;
            IF v_key IS NULL THEN
                RAISE EXCEPTION 'No Outbox primary key mapping for table %', TG_TABLE_NAME;
            END IF;

            SELECT transaction_sequence INTO v_transaction_sequence
            FROM app_outbox_transactions
            WHERE transaction_id = v_transaction_id
            FOR UPDATE;
            IF NOT FOUND THEN
                UPDATE app_outbox_clock
                   SET last_sequence = last_sequence + 1
                 WHERE singleton = TRUE
                 RETURNING last_sequence INTO v_transaction_sequence;
                INSERT INTO app_outbox_transactions (transaction_id, transaction_sequence, event_count)
                VALUES (v_transaction_id, v_transaction_sequence, 0);
            END IF;

            UPDATE app_outbox_transactions
               SET event_count = event_count + 1
             WHERE transaction_id = v_transaction_id
             RETURNING event_count INTO v_event_sequence;

            INSERT INTO app_outbox_events
                (transaction_id, event_sequence, table_name, operation, row_key, before_row, after_row)
            VALUES
                (v_transaction_id, v_event_sequence, TG_TABLE_NAME, TG_OP, v_key, v_before, v_after);
            RETURN NULL;
        END;
        $$
    """)
    pg_conn.execute("""
        CREATE OR REPLACE FUNCTION reject_app_outbox_truncate()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'TRUNCATE is disabled for Outbox-replicated table %', TG_TABLE_NAME;
        END;
        $$
    """)

    if not install_tables:
        return
    for table in SYNC_TABLE_PRIMARY_KEYS:
        exists = pg_conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (table,)).fetchone()
        if not exists or not exists["present"]:
            continue
        trigger_name = f"trg_app_outbox_{table}"
        pg_conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}" ON "{table}"')
        pg_conn.execute(
            f'''CREATE TRIGGER "{trigger_name}"
                AFTER INSERT OR UPDATE OR DELETE ON "{table}"
                FOR EACH ROW EXECUTE FUNCTION capture_app_outbox_row_change()'''
        )
        truncate_trigger_name = f"trg_app_outbox_no_truncate_{table}"
        pg_conn.execute(f'DROP TRIGGER IF EXISTS "{truncate_trigger_name}" ON "{table}"')
        pg_conn.execute(
            f'''CREATE TRIGGER "{truncate_trigger_name}"
                BEFORE TRUNCATE ON "{table}"
                FOR EACH STATEMENT EXECUTE FUNCTION reject_app_outbox_truncate()'''
        )

# ---------------------------------------------------------------------------
# OutboxManager
# ---------------------------------------------------------------------------

class OutboxManager:
    """Управление журналом изменений в SQLite."""

    def __init__(self, db_path: str | None = None) -> None:
        from database_manager import _sqlite_path
        self._db_path = db_path or _sqlite_path()

    def _connect(self) -> sqlite3.Connection:
        path = os.path.abspath(self._db_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if path not in _sqlite_schema_paths:
            with _sqlite_schema_lock:
                if path not in _sqlite_schema_paths:
                    from db_sqlite_schema import ensure_sqlite_schema
                    ensure_sqlite_schema(path)
                    _sqlite_schema_paths.add(path)
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        con.create_function("database_outbox_enabled", 0, lambda: 0)
        return con

    def add_operation(
        self,
        operation_type: str,
        payload: dict[str, Any],
        sync_status: str = "pending",
    ) -> str:
        """Записать операцию в Outbox. Возвращает ID операции."""
        op_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

        # Пометить superseded более ранние абсолютные операции того же типа
        con = self._connect()
        try:
            with con:
                if operation_type in SUPERSEDEABLE_OPS:
                    self._supersede_older(con, operation_type, payload)

                con.execute(
                    """INSERT INTO outbox_operations
                       (id, created_at, operation_type, payload, sync_status)
                       VALUES (?, ?, ?, ?, ?)""",
                    (op_id, created_at, operation_type, payload_json, sync_status),
                )
        finally:
            con.close()
        return op_id

    def _supersede_older(
        self,
        con: sqlite3.Connection,
        operation_type: str,
        payload: dict,
    ) -> None:
        """Пометить более ранние операции как superseded."""
        # Для set_user_card_quantity — уникальный ключ: (telegram_id, card_id)
        # Для set_user_card_quantities / clear_user_cards / set_desired_cards — (telegram_id,)
        if operation_type == "set_user_card_quantity":
            key_filter = json.dumps({
                "telegram_id": payload.get("telegram_id"),
                "card_id": payload.get("card_id"),
            }, sort_keys=True)
            # Нет простого способа фильтровать по JSON в SQLite без JSON1
            # Загружаем pending-операции того же типа и фильтруем в Python
            rows = con.execute(
                """SELECT id, payload FROM outbox_operations
                   WHERE operation_type = ? AND sync_status = 'pending'""",
                (operation_type,),
            ).fetchall()
            ids_to_supersede = []
            for row in rows:
                try:
                    p = json.loads(row["payload"])
                    if (
                        p.get("telegram_id") == payload.get("telegram_id")
                        and p.get("card_id") == payload.get("card_id")
                    ):
                        ids_to_supersede.append(row["id"])
                except (json.JSONDecodeError, TypeError):
                    pass
            for op_id in ids_to_supersede:
                con.execute(
                    "UPDATE outbox_operations SET sync_status='superseded' WHERE id=?",
                    (op_id,),
                )
        elif operation_type in {"set_user_card_quantities", "clear_user_cards", "set_desired_cards"}:
            rows = con.execute(
                """SELECT id, payload FROM outbox_operations
                   WHERE operation_type = ? AND sync_status = 'pending'""",
                (operation_type,),
            ).fetchall()
            ids_to_supersede = []
            for row in rows:
                try:
                    p = json.loads(row["payload"])
                    if p.get("telegram_id") == payload.get("telegram_id"):
                        ids_to_supersede.append(row["id"])
                except (json.JSONDecodeError, TypeError):
                    pass
            for op_id in ids_to_supersede:
                con.execute(
                    "UPDATE outbox_operations SET sync_status='superseded' WHERE id=?",
                    (op_id,),
                )

    def mark_synced(self, op_id: str) -> None:
        con = self._connect()
        try:
            with con:
                con.execute(
                    "UPDATE outbox_operations SET sync_status='synced' WHERE id=?",
                    (op_id,),
                )
        finally:
            con.close()

    def mark_failed(self, op_id: str, error: str, increment_attempts: bool = True) -> None:
        con = self._connect()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        try:
            with con:
                if increment_attempts:
                    con.execute(
                        """UPDATE outbox_operations
                           SET sync_status='failed',
                               error_message=?,
                               last_attempt_at=?,
                               sync_attempts=sync_attempts+1
                           WHERE id=?""",
                        (error[:500], now, op_id),
                    )
                else:
                    con.execute(
                        """UPDATE outbox_operations
                           SET error_message=?,
                               last_attempt_at=?,
                               sync_attempts=sync_attempts+1
                           WHERE id=?""",
                        (error[:500], now, op_id),
                    )
        finally:
            con.close()

    def reset_failed_to_pending(self, op_id: str) -> None:
        """Сбросить статус failed → pending для повторной попытки."""
        con = self._connect()
        try:
            with con:
                con.execute(
                    """UPDATE outbox_operations SET sync_status='pending'
                       WHERE id=? AND sync_status='failed'""",
                    (op_id,),
                )
        finally:
            con.close()

    def get_pending(self, limit: int = 500) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT rowid AS operation_sequence, * FROM outbox_operations
                   WHERE sync_status IN ('pending', 'failed')
                     AND sync_attempts < ?
                   ORDER BY rowid ASC""",
                (int(os.getenv("DB_SYNC_MAX_ATTEMPTS", "10")),),
            ).fetchall()
            now = datetime.now(timezone.utc)
            base_delay = max(1.0, float(os.getenv("DB_SYNC_RETRY_DELAY", "10")))
            max_delay = max(base_delay, float(os.getenv("DB_SYNC_MAX_RETRY_DELAY", "900")))
            ready = []
            current_group = None
            for row in rows:
                operation = dict(row)
                if (operation.get("error_message") or "").startswith("CONFLICT:"):
                    continue
                attempted = operation.get("last_attempt_at")
                if operation["sync_status"] == "failed" and attempted:
                    try:
                        last = datetime.fromisoformat(str(attempted).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        last = now
                    delay = min(max_delay, base_delay * (2 ** max(0, operation["sync_attempts"] - 1)))
                    if (now - last).total_seconds() < delay:
                        continue
                group = operation.get("transaction_id") or operation["id"]
                if ready and group != current_group and len(ready) >= max(1, limit):
                    break
                ready.append(operation)
                current_group = group
            return ready
        finally:
            con.close()

    def count_pending(self) -> int:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT COUNT(*) AS cnt FROM outbox_operations WHERE sync_status IN ('pending', 'failed')"
            ).fetchone()
            return int(row["cnt"]) if row else 0
        finally:
            con.close()


# ---------------------------------------------------------------------------
# SyncManager — синхронизация PG ↔ SQLite
# ---------------------------------------------------------------------------

class SyncManager:
    """Синхронизация данных между SQLite (Outbox) и PostgreSQL."""

    _sync_lock = threading.Lock()

    def __init__(self, db_path: str | None = None) -> None:
        from database_manager import _sqlite_path
        self._db_path = db_path or _sqlite_path()
        self._outbox = OutboxManager(self._db_path)
        self._pg_url = os.getenv("DATABASE_URL", "").strip()
        self._max_attempts = int(os.getenv("DB_SYNC_MAX_ATTEMPTS", "10"))

    def count_pending(self) -> int:
        return self._outbox.count_pending()

    def drain_postgres_outbox(self, batch_size: int = 100) -> int:
        """Apply batches until the observed PostgreSQL stream is caught up."""
        batch_size = max(1, int(batch_size))
        total = 0
        while True:
            applied = self.sync_postgres_outbox(batch_size)
            total += applied
            if applied < batch_size:
                return total

    def sync_postgres_outbox(self, limit: int = 100) -> int:
        """Apply committed PG transactions to SQLite, preserving each PG commit."""
        from db_sqlite_schema import SYNC_TABLE_PRIMARY_KEYS, ensure_sqlite_schema

        ensure_sqlite_schema(self._db_path)
        with self._sqlite_connection() as sl_conn:
            snapshot = sl_conn.execute(
                "SELECT value FROM database_metadata WHERE key='postgres_snapshot_complete'"
            ).fetchone()
            cursor = sl_conn.execute(
                "SELECT value FROM database_metadata WHERE key='pg_outbox_cursor'"
            ).fetchone()
        if not snapshot or not cursor:
            self.initial_sync_pg_to_sqlite()
            return 0
        applied = 0
        with self._pg_connection() as pg_conn:
            high_water = int(pg_conn.execute(
                "SELECT last_sequence FROM app_outbox_clock WHERE singleton=TRUE"
            ).fetchone()["last_sequence"])

            while applied < max(1, int(limit)):
                with self._sqlite_connection() as sl_conn:
                    cursor_row = sl_conn.execute(
                        "SELECT value FROM database_metadata WHERE key='pg_outbox_cursor'"
                    ).fetchone()
                    cursor = int(cursor_row["value"]) if cursor_row else 0
                if cursor > high_water:
                    raise OutboxGapError(
                        f"SQLite cursor {cursor} is ahead of PostgreSQL Outbox clock {high_water}"
                    )
                if cursor == high_water:
                    break

                expected_sequence = cursor + 1
                tx = pg_conn.execute(
                    """SELECT transaction_id, transaction_sequence, event_count
                       FROM app_outbox_transactions WHERE transaction_sequence=%s""",
                    (expected_sequence,),
                ).fetchone()
                if tx is None:
                    raise OutboxGapError(
                        f"PostgreSQL Outbox transaction sequence {expected_sequence} is missing"
                    )
                events = pg_conn.execute(
                    """SELECT event_sequence, table_name, operation, row_key, before_row, after_row
                       FROM app_outbox_events WHERE transaction_id=%s ORDER BY event_sequence""",
                    (tx["transaction_id"],),
                ).fetchall()
                expected_events = list(range(1, int(tx["event_count"]) + 1))
                if not expected_events:
                    raise OutboxGapError(
                        f"Outbox transaction {tx['transaction_id']} has no events"
                    )
                if [int(event["event_sequence"]) for event in events] != expected_events:
                    raise OutboxGapError(
                        f"Outbox transaction {tx['transaction_id']} has missing or reordered events"
                    )

                try:
                    with self._sqlite_connection() as sl_conn:
                        sl_conn.execute("BEGIN IMMEDIATE")
                        local_cursor = sl_conn.execute(
                            "SELECT value FROM database_metadata WHERE key='pg_outbox_cursor'"
                        ).fetchone()
                        current_cursor = int(local_cursor["value"]) if local_cursor else 0
                        if current_cursor != cursor:
                            if current_cursor > cursor:
                                continue
                            raise OutboxGapError("SQLite Outbox cursor moved backwards while applying a transaction")
                        receipt = sl_conn.execute(
                            """SELECT 1 FROM replicated_outbox_transactions
                               WHERE transaction_id=? AND transaction_sequence=?""",
                            (tx["transaction_id"], expected_sequence),
                        ).fetchone()
                        if receipt is None:
                            for event in events:
                                self._apply_pg_row_event(sl_conn, event, SYNC_TABLE_PRIMARY_KEYS)
                            sl_conn.execute(
                                """INSERT INTO replicated_outbox_transactions
                                   (transaction_id, transaction_sequence) VALUES (?, ?)""",
                                (tx["transaction_id"], expected_sequence),
                            )
                        sl_conn.execute(
                            """INSERT INTO database_metadata (key, value) VALUES ('pg_outbox_cursor', ?)
                               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                            (str(expected_sequence),),
                        )
                        sl_conn.commit()
                except Exception:
                    logger.exception(
                        "SQLite did not apply PostgreSQL Outbox transaction %s",
                        tx["transaction_id"],
                    )
                    raise
                applied += 1
        return applied

    def _apply_pg_row_event(self, sqlite_conn, event: dict, primary_keys: dict) -> None:
        table = event["table_name"]
        if table not in primary_keys:
            raise ValueError(f"Outbox references an unsupported table: {table}")
        operation = event["operation"]
        if operation not in {"INSERT", "UPDATE", "DELETE"}:
            raise ValueError(f"Unsupported PostgreSQL Outbox operation: {operation}")
        key = self._normalize_row(event["row_key"])
        before = self._normalize_row(event["before_row"])
        after = self._normalize_row(event["after_row"])
        pk_columns = primary_keys[table]
        if not isinstance(key, dict) or set(key) != set(pk_columns):
            raise ValueError(f"Invalid Outbox primary key for {table}")
        if (operation == "INSERT" and before is not None) or (operation == "DELETE" and after is not None):
            raise ValueError(f"Invalid row images for {table} {operation}")

        where = " AND ".join(f'"{column}"=?' for column in pk_columns)
        key_values = [key[column] for column in pk_columns]
        current_raw = sqlite_conn.execute(
            f'SELECT * FROM "{table}" WHERE {where}', key_values
        ).fetchone()
        current = self._normalize_row(dict(current_raw) if current_raw is not None else None)
        if current == after:
            return
        if current != before:
            raise RowChangeConflict(
                f"SQLite replica diverged at {table} key {key}; expected the PostgreSQL before image"
            )
        if operation == "DELETE":
            sqlite_conn.execute(f'DELETE FROM "{table}" WHERE {where}', key_values)
            return
        if not isinstance(after, dict):
            raise ValueError(f"Missing PostgreSQL after image for {table}")

        columns = [row[1] for row in sqlite_conn.execute(f'PRAGMA table_info("{table}")')]
        if set(after) - set(columns):
            raise ValueError(f"Outbox row has unknown columns for {table}")
        values = dict(after)
        if "properties" in values and not isinstance(values["properties"], str):
            values["properties"] = json.dumps(values["properties"], ensure_ascii=False, sort_keys=True)
        event_columns = list(values)
        if operation == "INSERT":
            column_sql = ", ".join(f'"{column}"' for column in event_columns)
            placeholders = ", ".join("?" for _ in event_columns)
            sqlite_conn.execute(
                f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
                [values[column] for column in event_columns],
            )
            return
        mutable = [column for column in event_columns if column not in pk_columns]
        if mutable:
            set_sql = ", ".join(f'"{column}"=?' for column in mutable)
            sqlite_conn.execute(
                f'UPDATE "{table}" SET {set_sql} WHERE {where}',
                [values[column] for column in mutable] + key_values,
            )

    # ------------------------------------------------------------------
    # Первоначальная синхронизация PG → SQLite
    # ------------------------------------------------------------------

    def initial_sync_pg_to_sqlite(self) -> None:
        """
        Синхронизировать состояние PG в SQLite при первом запуске.
        Если SQLite уже содержит данные — не стирает их (мягкое слияние).
        """
        from db_sqlite_schema import ensure_sqlite_schema, now_sqlite

        logger.info("Начальная синхронизация PG → SQLite")
        ensure_sqlite_schema(self._db_path)

        has_snapshot = False
        with self._sqlite_connection() as sl_conn:
            snapshot = sl_conn.execute(
                "SELECT value FROM database_metadata WHERE key='postgres_snapshot_complete'"
            ).fetchone()
            cursor = sl_conn.execute(
                "SELECT value FROM database_metadata WHERE key='pg_outbox_cursor'"
            ).fetchone()
            if snapshot and cursor:
                has_snapshot = True
            pending = sl_conn.execute(
                "SELECT COUNT(*) AS n FROM outbox_operations WHERE sync_status IN ('pending','failed')"
            ).fetchone()["n"]
            if pending and not has_snapshot:
                raise RuntimeError("Локальный Outbox содержит незавершённые записи; bootstrap остановлен без изменения данных")
        if has_snapshot:
            self.drain_postgres_outbox()
            return

        with self._pg_connection() as pg_conn:
            ensure_postgres_outbox_schema(pg_conn)

        with self._pg_connection() as pg_conn:
            pg_conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            with self._sqlite_connection() as sl_conn:
                # Синхронизируем таблицы в правильном порядке (FK)
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "cards",
                    pk="id",
                    columns=["id", "name", "rarity", "icon"])
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "users",
                    pk="id",
                    columns=["id", "telegram_id", "username", "first_name", "last_name",
                             "created_at", "last_seen_at", "last_activity_at"])
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "user_cards",
                    pk=None,
                    columns=["user_id", "card_id", "quantity"],
                    composite_pk=("user_id", "card_id"))
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "desired_cards",
                    pk=None,
                    columns=["user_id", "card_id", "created_at"],
                    composite_pk=("user_id", "card_id"))
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "trades",
                    pk="id",
                    columns=["id", "from_user_id", "to_user_id", "status", "created_at"])
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "trade_requests",
                    pk="id",
                    columns=["id", "sender_user_id", "recipient_user_id",
                             "offered_card_id", "requested_card_id",
                             "offered_card_name", "offered_card_icon",
                             "requested_card_name", "requested_card_icon",
                             "status", "sender_agreed_at", "recipient_agreed_at",
                             "sender_completed_at", "recipient_completed_at",
                             "exchange_code_sent_at", "created_at", "updated_at"])
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "processed_telegram_updates",
                    pk="update_id",
                    columns=["update_id", "processed_at"])
                self._sync_table_pg_to_sqlite(pg_conn, sl_conn, "user_analytics_events",
                    pk="id",
                    columns=["id", "user_id", "event_name", "properties", "created_at"],
                    transform_row=self._transform_analytics_row)

                snapshot_sequence = int(pg_conn.execute(
                    "SELECT last_sequence FROM app_outbox_clock WHERE singleton=TRUE"
                ).fetchone()["last_sequence"])
                violations = sl_conn.execute("PRAGMA foreign_key_check").fetchone()
                if violations:
                    raise RuntimeError("SQLite snapshot содержит нарушения внешних ключей")
                sl_conn.execute(
                    """INSERT INTO database_metadata (key, value) VALUES ('pg_outbox_cursor', ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (str(snapshot_sequence),),
                )
                sl_conn.execute(
                    """INSERT INTO database_metadata (key, value)
                       VALUES ('postgres_snapshot_complete', ?)
                       ON CONFLICT (key) DO UPDATE SET value=excluded.value""",
                    (now_sqlite(),),
                )

                sl_conn.commit()

        logger.info("Начальная синхронизация PG → SQLite завершена")

    def _transform_analytics_row(self, row: dict) -> dict:
        """Преобразовать JSONB-свойства в JSON-строку для SQLite."""
        import json as _json
        result = dict(row)
        props = result.get("properties", {})
        if isinstance(props, dict):
            result["properties"] = _json.dumps(props, ensure_ascii=False)
        elif props is None:
            result["properties"] = "{}"
        return result

    def _sync_table_pg_to_sqlite(
        self,
        pg_conn,
        sl_conn: sqlite3.Connection,
        table: str,
        pk: str | None,
        columns: list[str],
        composite_pk: tuple[str, ...] | None = None,
        transform_row=None,
        batch_size: int = 1000,
    ) -> None:
        """Seed an empty projection; reject local divergence instead of merging it."""
        from db_sqlite_schema import SYNC_TABLE_PRIMARY_KEYS

        logger.debug("Синхронизация таблицы %s", table)
        primary_key = composite_pk or SYNC_TABLE_PRIMARY_KEYS.get(table)
        if not primary_key:
            raise ValueError(f"No synchronization primary key for {table}")
        cols_str = ", ".join(columns)
        pg_conn.execute(f"SELECT {cols_str} FROM {table}")
        rows = pg_conn.fetchall()
        remote_rows = {}
        dirty_keys = set()
        for operation in sl_conn.execute(
            "SELECT operation_type, payload FROM outbox_operations WHERE sync_status IN ('pending','failed')"
        ).fetchall():
            if operation["operation_type"] != "sqlite_row_change":
                continue
            try:
                change = json.loads(operation["payload"])
                if change.get("table") == table:
                    dirty_keys.add(tuple(change.get("pk", {}).get(key) for key in primary_key))
            except (json.JSONDecodeError, TypeError):
                raise RuntimeError("Повреждённая запись row-change Outbox")

        placeholders = ", ".join("?" for _ in columns)
        conflict_target = ", ".join(f'"{key}"' for key in primary_key)
        mutable = [column for column in columns if column not in primary_key]
        update_sql = ", ".join(f'"{column}"=excluded."{column}"' for column in mutable)
        insert_sql = f'INSERT INTO "{table}" ({cols_str}) VALUES ({placeholders})'
        if mutable:
            insert_sql += f" ON CONFLICT ({conflict_target}) DO UPDATE SET {update_sql}"
        else:
            insert_sql += f" ON CONFLICT ({conflict_target}) DO NOTHING"

        for i in range(0, len(rows), batch_size):
            for row in rows[i:i + batch_size]:
                values_row = dict(row)
                if transform_row:
                    values_row = transform_row(values_row)
                values = []
                for col in columns:
                    val = values_row.get(col)
                    if isinstance(val, datetime):
                        val = val.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    elif hasattr(val, "isoformat"):
                        val = val.isoformat()
                    values.append(val)
                key = tuple(values_row.get(field) for field in primary_key)
                remote_rows[key] = dict(zip(columns, values))

        # Existing local content is never silently overwritten or deleted.
        pk_sql = " AND ".join(f'"{key}"=?' for key in primary_key)
        local_rows = sl_conn.execute(f'SELECT {cols_str} FROM "{table}"').fetchall()
        for local in local_rows:
            local_map = dict(local)
            key = tuple(local_map[field] for field in primary_key)
            remote = remote_rows.get(key)
            if remote is None:
                raise RowChangeConflict(f"SQLite bootstrap found local-only row in {table} key={key}")
            if self._normalize_row(local_map) != self._normalize_row(remote):
                raise RowChangeConflict(f"SQLite bootstrap found divergent row in {table} key={key}")

        for key, remote in remote_rows.items():
            exists = sl_conn.execute(f'SELECT 1 FROM "{table}" WHERE {pk_sql}', key).fetchone()
            if exists is None:
                sl_conn.execute(insert_sql, [remote[col] for col in columns])

        logger.debug("Таблица %s: %d строк PostgreSQL объединено", table, len(rows))

    # ------------------------------------------------------------------
    # Синхронизация Outbox → PG
    # ------------------------------------------------------------------

    def sync_pending_outbox(self) -> None:
        """Stage 1 forbids replaying local writes into the PostgreSQL primary."""
        raise RuntimeError("SQLite→PostgreSQL replay отключён; локальный режим записи не поддерживается")

    def _try_acquire_sync_lock(self) -> bool:
        """Попытаться захватить блокировку синхронизации в SQLite."""
        import os as _os
        pid = _os.getpid()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        con = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT holder_pid FROM sync_lock WHERE lock_key='sync'"
            ).fetchone()
            if current:
                holder_pid = current[0]
                alive = True
                if holder_pid and holder_pid != pid:
                    try:
                        os.kill(int(holder_pid), 0)
                    except ProcessLookupError:
                        alive = False
                    except (PermissionError, OSError):
                        alive = True
                if alive:
                    con.rollback()
                    return False
                con.execute("DELETE FROM sync_lock WHERE lock_key='sync'")
            con.execute(
                "INSERT INTO sync_lock (lock_key, holder_pid, acquired_at) VALUES ('sync', ?, ?)",
                (pid, now),
            )
            con.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        except Exception as exc:
            logger.warning("Ошибка захвата sync_lock: %s", exc)
            return False
        finally:
            con.close()

    def _release_sync_lock(self) -> None:
        import os as _os
        pid = _os.getpid()
        con = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
        try:
            con.execute(
                "DELETE FROM sync_lock WHERE lock_key='sync' AND holder_pid=?",
                (pid,),
            )
            con.commit()
        except Exception:
            pass
        finally:
            con.close()

    def _do_sync_outbox(self) -> None:
        """Внутренняя логика синхронизации Outbox → PG."""
        pending = self._outbox.get_pending()
        logger.info("Синхронизация Outbox: %d операций", len(pending))

        with self._pg_connection() as pg_conn:
            # Убедиться что таблица idempotency существует
            pg_conn.execute("""
                CREATE TABLE IF NOT EXISTS outbox_applied (
                    op_id TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            pg_conn.commit()

            groups: dict[str, list[dict]] = {}
            for operation in pending:
                group_id = operation.get("transaction_id") or operation["id"]
                groups.setdefault(group_id, []).append(operation)

            failed = False
            for group in groups.values():
                unsynced_ids = []
                try:
                    for op in group:
                        op_id = op["id"]
                        try:
                            payload = json.loads(op["payload"])
                        except (json.JSONDecodeError, TypeError) as exc:
                            raise ValueError("invalid_payload") from exc
                        marker = pg_conn.execute(
                            "SELECT op_id FROM outbox_applied WHERE op_id=%s", (op_id,)
                        ).fetchone()
                        if marker:
                            continue
                        self._apply_operation(pg_conn, op["operation_type"], payload)
                        pg_conn.execute(
                            "INSERT INTO outbox_applied (op_id) VALUES (%s) ON CONFLICT DO NOTHING",
                            (op_id,),
                        )
                        unsynced_ids.append(op_id)
                    pg_conn.commit()
                    for op in group:
                        self._outbox.mark_synced(op["id"])
                except Exception as exc:
                    pg_conn.rollback()
                    if isinstance(exc, RowChangeConflict):
                        logger.error("[DB_EVENT] DATABASE_CONFLICT_DETECTED operation_id=%s", group[0]["id"])
                    for op in group:
                        self._outbox.mark_failed(op["id"], str(exc)[:500])
                    failed = True

            if failed or self._outbox.count_pending():
                raise RuntimeError("Остались операции Outbox, которые не подтверждены PostgreSQL")

    def _apply_operation(self, pg_conn, op_type: str, payload: dict) -> None:
        """Применить одну операцию к PostgreSQL (идемпотентно по смыслу)."""
        if op_type == "sqlite_row_change":
            self._apply_sqlite_row_change(pg_conn, payload)
        elif op_type == "set_user_card_quantity":
            self._apply_set_user_card_quantity(pg_conn, payload)
        elif op_type == "set_user_card_quantities":
            self._apply_set_user_card_quantities(pg_conn, payload)
        elif op_type == "clear_user_cards":
            self._apply_clear_user_cards(pg_conn, payload)
        elif op_type == "set_desired_cards":
            self._apply_set_desired_cards(pg_conn, payload)
        elif op_type == "get_or_create_user":
            self._apply_get_or_create_user(pg_conn, payload)
        elif op_type == "add_zero_cards":
            self._apply_add_zero_cards(pg_conn, payload)
        elif op_type == "record_analytics_events":
            self._apply_record_analytics_events(pg_conn, payload)
        elif op_type == "create_trade_request":
            self._apply_create_trade_request(pg_conn, payload)
        elif op_type == "transition_trade_request":
            self._apply_transition_trade_request(pg_conn, payload)
        elif op_type == "remember_processed_update":
            self._apply_remember_processed_update(pg_conn, payload)
        else:
            raise ValueError(f"Unknown outbox operation type: {op_type}")

    @staticmethod
    def _normalize_row(row: dict | None) -> dict | None:
        if row is None:
            return None
        normalized = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                value = value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            elif key.endswith("_at") and isinstance(value, str) and value:
                try:
                    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    value = stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                except ValueError:
                    pass
            elif key == "properties" and isinstance(value, str):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    pass
            normalized[key] = value
        return normalized

    def _apply_sqlite_row_change(self, pg_conn, payload: dict) -> None:
        from db_sqlite_schema import SYNC_TABLE_PRIMARY_KEYS

        table = payload.get("table")
        action = payload.get("action")
        if table not in SYNC_TABLE_PRIMARY_KEYS or action not in {"insert", "update", "delete"}:
            raise ValueError("Invalid SQLite row-change payload")
        primary_key = SYNC_TABLE_PRIMARY_KEYS[table]
        pk = payload.get("pk")
        before = self._normalize_row(payload.get("before"))
        after = self._normalize_row(payload.get("after"))
        if not isinstance(pk, dict) or set(pk) != set(primary_key):
            raise ValueError("SQLite row-change payload has an invalid primary key")
        if (action == "delete") != (after is None) or (action == "insert") != (before is None):
            raise ValueError("SQLite row-change payload has inconsistent row images")

        key_where = " AND ".join(f'"{key}"=%s' for key in primary_key)
        pg_conn.execute(f'SELECT * FROM "{table}" WHERE {key_where}', [pk[key] for key in primary_key])
        current_row = pg_conn.fetchone()
        current = self._normalize_row(dict(current_row) if current_row else None)
        if current == after:
            return
        if current != before:
            raise RowChangeConflict(f"CONFLICT: row diverged in table {table}")

        if action == "delete":
            pg_conn.execute(f'DELETE FROM "{table}" WHERE {key_where}', [pk[key] for key in primary_key])
            return
        if not isinstance(after, dict):
            raise ValueError("SQLite row-change payload is missing the new row image")
        columns = list(after)
        if action == "insert":
            col_sql = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            pg_conn.execute(
                f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})',
                [self._pg_value(column, after[column]) for column in columns],
            )
        else:
            mutable = [column for column in columns if column not in primary_key]
            if mutable:
                set_sql = ", ".join(f'"{column}"=%s' for column in mutable)
                values = [self._pg_value(column, after[column]) for column in mutable] + [pk[key] for key in primary_key]
                pg_conn.execute(f'UPDATE "{table}" SET {set_sql} WHERE {key_where}', values)

        if table in {"users", "trades", "user_analytics_events", "trade_requests"} and "id" in after:
            pg_conn.execute(
                "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                f"GREATEST(COALESCE((SELECT MAX(id) FROM \"{table}\"), 1), 1), true)",
                (table,),
            )

    @staticmethod
    def _pg_value(column: str, value: Any) -> Any:
        if column == "properties" and isinstance(value, (dict, list)):
            from psycopg.types.json import Jsonb
            return Jsonb(value)
        return value

    def _apply_set_user_card_quantity(self, pg_conn, payload: dict) -> None:
        telegram_id = payload["telegram_id"]
        card_id = payload["card_id"]
        quantity = payload["quantity"]
        pg_conn.execute(
            """
            INSERT INTO user_cards (user_id, card_id, quantity)
            SELECT id, %s, %s FROM users WHERE telegram_id = %s
            ON CONFLICT (user_id, card_id) DO UPDATE SET quantity = EXCLUDED.quantity
            """,
            (card_id, quantity, telegram_id),
        )
        if quantity > 0:
            pg_conn.execute(
                """
                DELETE FROM desired_cards
                WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                  AND card_id = %s
                """,
                (telegram_id, card_id),
            )

    def _apply_set_user_card_quantities(self, pg_conn, payload: dict) -> None:
        telegram_id = payload["telegram_id"]
        quantities = payload["quantities"]  # {card_id: quantity}
        if not quantities:
            return
        card_ids = list(quantities.keys())
        card_quantities = [quantities[k] for k in card_ids]
        owned_ids = [k for k, v in quantities.items() if v > 0]

        pg_conn.execute(
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
        if owned_ids:
            pg_conn.execute(
                """
                DELETE FROM desired_cards
                WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                  AND card_id = ANY(%s)
                """,
                (telegram_id, owned_ids),
            )

    def _apply_clear_user_cards(self, pg_conn, payload: dict) -> None:
        telegram_id = payload["telegram_id"]
        pg_conn.execute(
            """
            UPDATE user_cards SET quantity = 0
            WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
              AND quantity <> 0
            """,
            (telegram_id,),
        )

    def _apply_set_desired_cards(self, pg_conn, payload: dict) -> None:
        telegram_id = payload["telegram_id"]
        card_ids = payload["card_ids"]
        pg_conn.execute(
            "DELETE FROM desired_cards WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)",
            (telegram_id,),
        )
        if card_ids:
            pg_conn.execute(
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
                (card_ids, telegram_id),
            )

    def _apply_get_or_create_user(self, pg_conn, payload: dict) -> None:
        pg_conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, last_seen_at, last_activity_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = COALESCE(NULLIF(EXCLUDED.username, ''), users.username),
                first_name = COALESCE(NULLIF(EXCLUDED.first_name, ''), users.first_name),
                last_name = COALESCE(NULLIF(EXCLUDED.last_name, ''), users.last_name),
                last_seen_at = NOW(),
                last_activity_at = NOW()
            """,
            (
                payload["telegram_id"],
                payload.get("username", ""),
                payload.get("first_name", ""),
                payload.get("last_name", ""),
            ),
        )

    def _apply_add_zero_cards(self, pg_conn, payload: dict) -> None:
        telegram_id = payload["telegram_id"]
        card_ids = payload["card_ids"]
        if not card_ids:
            return
        pg_conn.execute(
            """
            INSERT INTO user_cards (user_id, card_id, quantity)
            SELECT u.id, c.id, 0
            FROM users u
            JOIN cards c ON c.id = ANY(%s)
            WHERE u.telegram_id = %s
            ON CONFLICT (user_id, card_id) DO NOTHING
            """,
            (card_ids, telegram_id),
        )

    def _apply_record_analytics_events(self, pg_conn, payload: dict) -> None:
        import json as _json
        user_id = payload["user_id"]
        events = payload["events"]
        for event_name, properties in events:
            props_json = _json.dumps(properties, ensure_ascii=False) if isinstance(properties, dict) else properties
            pg_conn.execute(
                """
                INSERT INTO user_analytics_events (user_id, event_name, properties)
                VALUES (%s, %s, %s::jsonb)
                """,
                (user_id, event_name, props_json),
            )

    def _apply_create_trade_request(self, pg_conn, payload: dict) -> None:
        # Используем существующие данные из payload (уже проверенные)
        # INSERT с полными данными, CONFLICT по составному ключу — пропускаем
        pg_conn.execute(
            """
            INSERT INTO trade_requests
                (sender_user_id, recipient_user_id, offered_card_id, requested_card_id,
                 offered_card_name, offered_card_icon, requested_card_name, requested_card_icon,
                 status, created_at, updated_at)
            SELECT sender.id, recipient.id, %s, %s, %s, %s, %s, %s, 'pending', NOW(), NOW()
            FROM users sender
            JOIN users recipient ON recipient.telegram_id = %s
            WHERE sender.telegram_id = %s
            ON CONFLICT DO NOTHING
            """,
            (
                payload["offered_card_id"],
                payload["requested_card_id"],
                payload["offered_card_name"],
                payload.get("offered_card_icon", ""),
                payload["requested_card_name"],
                payload.get("requested_card_icon", ""),
                payload["recipient_telegram_id"],
                payload["sender_telegram_id"],
            ),
        )

    def _apply_transition_trade_request(self, pg_conn, payload: dict) -> None:
        # Статус-переходы применяем осторожно — только если текущий статус совместим
        request_id = payload["request_id"]
        new_status = payload["new_status"]
        allowed_from = payload.get("allowed_from_statuses", [])
        if allowed_from:
            placeholders = ",".join(["%s"] * len(allowed_from))
            pg_conn.execute(
                f"UPDATE trade_requests SET status=%s, updated_at=NOW() "
                f"WHERE id=%s AND status IN ({placeholders})",
                [new_status, request_id] + list(allowed_from),
            )
        else:
            pg_conn.execute(
                "UPDATE trade_requests SET status=%s, updated_at=NOW() WHERE id=%s",
                (new_status, request_id),
            )

    def _apply_remember_processed_update(self, pg_conn, payload: dict) -> None:
        update_id = payload["update_id"]
        pg_conn.execute(
            "INSERT INTO processed_telegram_updates (update_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (update_id,),
        )

    # ------------------------------------------------------------------
    # Проверка целостности
    # ------------------------------------------------------------------

    def verify_integrity(self) -> bool:
        """Compare complete mirrored row images and verify referential integrity."""
        tables = [
            "users", "cards", "user_cards", "desired_cards", "trades",
            "trade_requests", "processed_telegram_updates", "user_analytics_events",
        ]
        try:
            if self._outbox.count_pending() != 0:
                return False
            with self._pg_connection() as pg_conn:
                pg_fingerprints = {}
                pg_counts = {}
                for table in tables:
                    order_by = ", ".join(f'"{key}"' for key in __import__("db_sqlite_schema").SYNC_TABLE_PRIMARY_KEYS[table])
                    cursor = pg_conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_by}')
                    digest = hashlib.sha256()
                    count = 0
                    for row in cursor:
                        normalized = self._normalize_row(dict(row))
                        digest.update(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode())
                        digest.update(b"\n")
                        count += 1
                    pg_counts[table] = count
                    pg_fingerprints[table] = digest.hexdigest()

                foreign_key_count = pg_conn.execute(
                    """SELECT COUNT(*) AS count FROM pg_constraint c
                       JOIN pg_class r ON r.oid=c.conrelid
                       WHERE c.contype='f' AND r.relname = ANY(%s)""",
                    (tables,),
                ).fetchone()["count"]
                orphan_checks = (
                    ("user_cards", "SELECT EXISTS (SELECT 1 FROM user_cards uc LEFT JOIN users u ON u.id=uc.user_id LEFT JOIN cards c ON c.id=uc.card_id WHERE u.id IS NULL OR c.id IS NULL)"),
                    ("desired_cards", "SELECT EXISTS (SELECT 1 FROM desired_cards dc LEFT JOIN users u ON u.id=dc.user_id LEFT JOIN cards c ON c.id=dc.card_id WHERE u.id IS NULL OR c.id IS NULL)"),
                    ("trade_requests", "SELECT EXISTS (SELECT 1 FROM trade_requests tr LEFT JOIN users s ON s.id=tr.sender_user_id LEFT JOIN users r ON r.id=tr.recipient_user_id WHERE s.id IS NULL OR r.id IS NULL)"),
                    ("user_analytics_events", "SELECT EXISTS (SELECT 1 FROM user_analytics_events a LEFT JOIN users u ON u.id=a.user_id WHERE u.id IS NULL)"),
                    ("trades", "SELECT EXISTS (SELECT 1 FROM trades t LEFT JOIN users s ON s.id=t.from_user_id LEFT JOIN users r ON r.id=t.to_user_id WHERE s.id IS NULL OR r.id IS NULL)"),
                )
                if int(foreign_key_count) < 9:
                    logger.error("PostgreSQL: недостаточно внешних ключей в зеркальных таблицах")
                    return False
                for _, query in orphan_checks:
                    if bool(pg_conn.execute(query).fetchone()[0]):
                        logger.error("PostgreSQL: найдены строки с нарушенными связями")
                        return False

            from db_sqlite_schema import SYNC_TABLE_PRIMARY_KEYS
            with self._sqlite_connection() as sl_conn:
                if sl_conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    return False
                if sl_conn.execute("PRAGMA foreign_key_check").fetchone():
                    return False
                sqlite_fk_count = sum(
                    len(sl_conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall())
                    for table in tables
                )
                if sqlite_fk_count < 9:
                    return False
                for table in tables:
                    order_by = ", ".join(f'"{key}"' for key in SYNC_TABLE_PRIMARY_KEYS[table])
                    cursor = sl_conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_by}')
                    digest = hashlib.sha256()
                    count = 0
                    for row in cursor:
                        normalized = self._normalize_row(dict(row))
                        digest.update(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode())
                        digest.update(b"\n")
                        count += 1
                    if count != pg_counts[table] or digest.hexdigest() != pg_fingerprints[table]:
                        logger.warning("Проверка содержимого не прошла для таблицы %s", table)
                        return False
                    logger.info("Проверка содержимого пройдена: %s (%d строк)", table, count)
            return True
        except Exception as exc:
            logger.error("Ошибка проверки целостности: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Внутренние соединения
    # ------------------------------------------------------------------

    @contextmanager
    def _pg_connection(self):
        from database_manager import get_manager
        with get_manager().get_pg_connection() as conn:
            yield conn

    @contextmanager
    def _sqlite_connection(self):
        from db_sqlite_schema import ensure_sqlite_schema
        path = os.path.abspath(self._db_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if path not in _sqlite_schema_paths:
            with _sqlite_schema_lock:
                if path not in _sqlite_schema_paths:
                    ensure_sqlite_schema(path)
                    _sqlite_schema_paths.add(path)
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.create_function("database_outbox_enabled", 0, lambda: 0)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
