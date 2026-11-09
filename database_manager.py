"""PostgreSQL primary manager with a read-only local SQLite projection."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)
_sqlite_schema_lock = threading.Lock()
_sqlite_schema_paths: set[str] = set()

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def _sqlite_path() -> str:
    return os.getenv("SQLITE_DATABASE_PATH", "/data/pubg_local.db").strip()


def _pg_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _connect_timeout() -> float:
    return max(1.0, float(os.getenv("DB_FAILOVER_CONNECT_TIMEOUT", "5")))


def _check_interval() -> float:
    return max(10.0, float(os.getenv("DB_FAILOVER_CHECK_INTERVAL", "30")))


# ---------------------------------------------------------------------------
# Режимы БД
# ---------------------------------------------------------------------------

class DatabaseMode(Enum):
    POSTGRES = "postgres"
    SQLITE = "sqlite"


# ---------------------------------------------------------------------------
# События логирования (структурированные)
# ---------------------------------------------------------------------------

def _log_event(event: str, **kwargs) -> None:
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info("[DB_EVENT] %s %s", event, extra)


# ---------------------------------------------------------------------------
# Singleton — DatabaseManager
# ---------------------------------------------------------------------------

class DatabaseManager:
    """Thread-safe менеджер переключения между PostgreSQL и SQLite."""

    _instance: "DatabaseManager | None" = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "DatabaseManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._mode = DatabaseMode.POSTGRES
        self._mode_lock = threading.RLock()
        self._pg_available = False
        self._last_pg_check: float = 0.0
        self._last_sync_at: float | None = None
        self._sync_error_count = 0
        self._pending_outbox_count = 0
        self._switch_reason: str = ""
        self._monitor_thread: threading.Thread | None = None
        self._stop_monitor = threading.Event()
        self._initialized = False

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Инициализировать менеджер: SQLite-схема, проверить PG, выбрать режим."""
        from db_sqlite_schema import ensure_sqlite_schema
        from db_sync import SyncManager

        sqlite_db = _sqlite_path()
        logger.info("Инициализация DatabaseManager, SQLite: %s", sqlite_db)

        # Создать схему SQLite (идемпотентно)
        try:
            ensure_sqlite_schema(sqlite_db)
        except (sqlite3.DatabaseError, OSError) as exc:
            raise RuntimeError(
                f"Локальная SQLite недоступна или повреждена ({sqlite_db}): {exc}"
            ) from exc

        pg_url = _pg_url()
        if not pg_url:
            raise RuntimeError("DATABASE_URL не задан: PostgreSQL остаётся единственным источником записей")

        # Проверить доступность PG
        if self._check_pg_health():
            _log_event("DATABASE_POSTGRES_CONNECTED")
            self._pg_available = True

            # Mirror errors are visible and tracked; PG remains authoritative.
            try:
                # Install every replicated table and its trigger before taking
                # the snapshot, including analytics and trade webhook tables.
                import database
                import analytics
                import trade_requests
                database.init_db()
                analytics.init_analytics_db()
                trade_requests.init_trade_request_schema()
                sync = SyncManager()
                sync.initial_sync_pg_to_sqlite()
            except Exception as exc:
                self._sync_error_count += 1
                logger.exception("Начальная синхронизация PG→SQLite не выполнена")

            self._set_mode(DatabaseMode.POSTGRES, "PG доступна при старте")
        else:
            _log_event("DATABASE_POSTGRES_UNAVAILABLE", reason="startup_check_failed")
            logger.warning("PostgreSQL недоступна; запросы к БД будут завершаться ошибкой")

        self._initialized = True
        self._start_monitor()

    def _sqlite_has_schema(self) -> bool:
        """Проверить целостность SQLite и наличие минимальной локальной копии."""
        sqlite_db = _sqlite_path()
        import os as _os
        if not _os.path.exists(sqlite_db):
            return False
        try:
            con = sqlite3.connect(sqlite_db, timeout=3)
            try:
                integrity = con.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    return False
                tables = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                required = {
                    "database_metadata", "users", "cards", "user_cards",
                    "desired_cards", "outbox_operations",
                }
                if not required.issubset(tables):
                    return False
                snapshot = con.execute(
                    "SELECT value FROM database_metadata WHERE key='postgres_snapshot_complete'"
                ).fetchone()
                return snapshot is not None and bool(snapshot[0])
            finally:
                con.close()
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Переключение режимов
    # ------------------------------------------------------------------

    def _set_mode(self, mode: DatabaseMode, reason: str) -> None:
        if mode is not DatabaseMode.POSTGRES:
            raise RuntimeError("SQLite fallback отключён: PostgreSQL является единственной основной БД")
        with self._mode_lock:
            if self._mode != mode:
                old = self._mode.value
                self._mode = mode
                self._switch_reason = reason
                logger.info("Режим БД: %s → %s (%s)", old, mode.value, reason)
            else:
                self._mode = mode
                self._switch_reason = reason

    @property
    def mode(self) -> DatabaseMode:
        with self._mode_lock:
            return self._mode

    @property
    def is_postgres(self) -> bool:
        return self.mode == DatabaseMode.POSTGRES

    # ------------------------------------------------------------------
    # Проверка здоровья PG
    # ------------------------------------------------------------------

    def _check_pg_health(self) -> bool:
        pg_url = _pg_url()
        if not pg_url:
            return False
        timeout = _connect_timeout()
        try:
            conn = psycopg.connect(
                pg_url,
                connect_timeout=max(1, int(timeout)),
                options="-c statement_timeout=4000 -c lock_timeout=3000",
            )
            with conn:
                conn.execute("SELECT 1")
            conn.close()
            self._last_pg_check = time.monotonic()
            return True
        except Exception as exc:
            logger.debug("PG health check failed: %s", exc)
            self._last_pg_check = time.monotonic()
            return False

    # ------------------------------------------------------------------
    # Фоновый монитор
    # ------------------------------------------------------------------

    def _start_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="db-failover-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        interval = _check_interval()
        while not self._stop_monitor.wait(timeout=interval):
            try:
                self._run_health_cycle()
            except Exception as exc:
                logger.error("Ошибка в db-monitor: %s", exc)

    def _run_health_cycle(self) -> None:
        from db_sync import SyncManager

        pg_ok = self._check_pg_health()

        if not pg_ok:
            _log_event("DATABASE_POSTGRES_UNAVAILABLE", reason="health_check_failed")
            self._pg_available = False
            return
        was_available = self._pg_available
        self._pg_available = True
        try:
            applied = SyncManager().drain_postgres_outbox()
            self._pending_outbox_count = 0
            self._last_sync_at = time.monotonic()
            self._sync_error_count = 0
            if not was_available:
                _log_event("DATABASE_POSTGRES_RESTORED")
            _log_event("DATABASE_SYNC_COMPLETED", applied=applied)
        except Exception as exc:
            self._sync_error_count += 1
            _log_event("DATABASE_SYNC_FAILED", error=str(exc)[:200])
            logger.exception("PostgreSQL доступна, но синхронизация SQLite не выполнена")

    def stop_monitor(self) -> None:
        self._stop_monitor.set()

    # ------------------------------------------------------------------
    # Получение соединений
    # ------------------------------------------------------------------

    @contextmanager
    def get_pg_connection(self) -> Iterator[psycopg.Connection]:
        """Прямое соединение с PostgreSQL (для синхронизации и инициализации)."""
        pg_url = _pg_url()
        if not pg_url:
            raise RuntimeError("DATABASE_URL не задан")
        conn = psycopg.connect(
            pg_url,
            row_factory=dict_row,
            connect_timeout=max(1, int(_connect_timeout())),
            options="-c statement_timeout=10000 -c lock_timeout=3000",
        )
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def get_sqlite_connection(self) -> Iterator[sqlite3.Connection]:
        """Соединение с SQLite (WAL-режим, dict-row через row_factory)."""
        path = _sqlite_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        canonical_path = os.path.abspath(path)
        if canonical_path not in _sqlite_schema_paths:
            with _sqlite_schema_lock:
                if canonical_path not in _sqlite_schema_paths:
                    from db_sqlite_schema import ensure_sqlite_schema
                    ensure_sqlite_schema(canonical_path)
                    _sqlite_schema_paths.add(canonical_path)
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        from db_sqlite_schema import setup_sqlite_functions
        setup_sqlite_functions(con, outbox_enabled=False)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Статус для диагностики
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "mode": self.mode.value,
            "pg_available": self._pg_available,
            "switch_reason": self._switch_reason,
            "pending_outbox_count": self._pending_outbox_count,
            "last_sync_at": self._last_sync_at,
            "sync_error_count": self._sync_error_count,
            "sqlite_path": _sqlite_path(),
        }

    def notify_pg_write_failed(self, exc: Exception) -> None:
        """Report PG outage without changing the authoritative write target."""
        _log_event("DATABASE_POSTGRES_UNAVAILABLE", reason=str(exc)[:200])
        self._pg_available = False

    def invalidate_sqlite_snapshot(self, reason: str) -> None:
        """Block failover until a fresh PostgreSQL snapshot is completed."""
        try:
            with self.get_sqlite_connection() as con:
                con.execute("DELETE FROM database_metadata WHERE key='postgres_snapshot_complete'")
                con.commit()
            logger.critical("Локальная SQLite-копия помечена неготовой: %s", reason[:200])
        except Exception as exc:
            logger.critical("Не удалось пометить SQLite-копию неготовой: %s", exc)


# ---------------------------------------------------------------------------
# Удобный доступ к глобальному менеджеру
# ---------------------------------------------------------------------------

def get_manager() -> DatabaseManager:
    return DatabaseManager.get_instance()
