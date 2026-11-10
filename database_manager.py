<<<<<<< LOCAL (Your changes)
<<<<<<< LOCAL (Your changes)
"""Local SQLite connection manager with optional PostgreSQL snapshots."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

logger = logging.getLogger(__name__)
_sqlite_schema_lock = threading.Lock()
_sqlite_schema_paths: set[str] = set()


def _sqlite_path() -> str:
    path = os.getenv("SQLITE_DATABASE_PATH", "").strip()
    if path:
        return path
    if os.path.exists("/home/container/data") or os.path.exists("/home/container"):
        return "/home/container/data/pubg_local.db"
    return os.path.join(".", "data", "pubg_local.db")


def _pg_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _connect_timeout() -> float:
    return max(1.0, float(os.getenv("DB_FAILOVER_CONNECT_TIMEOUT", "5")))


class DatabaseMode(Enum):
    SQLITE = "sqlite"


class DatabaseManager:
    """SQLite is authoritative; PostgreSQL receives delayed full snapshots."""

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
        self._mode = DatabaseMode.SQLITE
        self._mode_lock = threading.RLock()
        self._initialized = False
        self._dirty = threading.Event()
        self._stop_backup = threading.Event()
        self._backup_thread: threading.Thread | None = None
        self._last_sync_at: float | None = None
        self._sync_error_count = 0
        self._switch_reason = "Локальная SQLite — основная база"

    def initialize(self) -> None:
        from db_sqlite_schema import ensure_sqlite_schema

        sqlite_db = _sqlite_path()
        existed = os.path.isfile(sqlite_db) and os.path.getsize(sqlite_db) > 0
        logger.info("Инициализация локальной SQLite: %s", sqlite_db)
        try:
            ensure_sqlite_schema(sqlite_db)
        except (sqlite3.DatabaseError, OSError) as exc:
            raise RuntimeError(f"Локальная SQLite недоступна или повреждена ({sqlite_db}): {exc}") from exc

        self._initialized = True
        if not existed:
            if not _pg_url():
                logger.info("[DB] PostgreSQL не настроен, работаем только локально")
                logger.warning("[DB] локальная БД отсутствует, PostgreSQL недоступен (причина: DATABASE_URL не задан)")
                logger.warning("[DB] создана пустая локальная БД, требуется ручное восстановление")
            else:
                self.restore_from_pg_if_missing(force=True)

        self._start_backup_thread()

    def restore_from_pg_if_missing(self, force: bool = False) -> int:
        sqlite_db = _sqlite_path()
        if not force and os.path.isfile(sqlite_db) and os.path.getsize(sqlite_db) > 0:
            try:
                with sqlite3.connect(sqlite_db, timeout=10) as con:
                    if con.execute("PRAGMA quick_check").fetchone()[0] == "ok":
                        return 0
            except sqlite3.Error:
                pass
            # Existing file failed quick_check; restore it from PostgreSQL.
        try:
            from db_sync import restore_snapshot_from_pg
            restored = restore_snapshot_from_pg(sqlite_db)
            if restored < 1:
                raise RuntimeError("снапшот PostgreSQL пуст")
            logger.info("[DB] локальная БД восстановлена из PostgreSQL (%s строк)", restored)
            return restored
        except Exception as exc:
            logger.warning("[DB] локальная БД отсутствует, PostgreSQL недоступен (причина: %s)", exc)
            logger.warning("[DB] создана пустая локальная БД, требуется ручное восстановление")
            return 0

    def _start_backup_thread(self) -> None:
        if not _pg_url() or self._backup_thread and self._backup_thread.is_alive():
            return
        self._stop_backup.clear()
        self._backup_thread = threading.Thread(
            target=self._backup_loop, name="database-snapshot-backup", daemon=True
        )
        self._backup_thread.start()

    def _backup_loop(self) -> None:
        debounce_seconds = max(5.0, min(10.0, float(os.getenv("DB_BACKUP_DEBOUNCE", "7"))))
        while not self._stop_backup.is_set():
            if not self._dirty.wait(timeout=1):
                continue
            self._dirty.clear()
            deadline = time.monotonic() + debounce_seconds
            while not self._stop_backup.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._dirty.wait(timeout=remaining):
                    self._dirty.clear()
                    deadline = time.monotonic() + debounce_seconds
            if self._stop_backup.is_set():
                return
            self._dirty.clear()
            try:
                from db_sync import backup_snapshot_to_pg
                rows = backup_snapshot_to_pg()
                self._last_sync_at = time.time()
                logger.info("[DB] PostgreSQL backup snapshot completed (%s строк)", rows)
            except Exception as exc:
                self._sync_error_count += 1
                logger.warning("[DB] ошибка резервного копирования PostgreSQL: %s", exc)
                self._dirty.set()
                self._stop_backup.wait(10)

    def mark_dirty(self) -> None:
        self._dirty.set()

    def has_pending_backup(self) -> bool:
        """True if there are unflushed SQLite changes and PostgreSQL is configured."""
        return self._dirty.is_set() and bool(_pg_url())

    def stop_backup(self) -> None:
        self._stop_backup.set()
        if self._backup_thread and self._backup_thread.is_alive():
            self._backup_thread.join(timeout=2)

    @property
    def mode(self) -> DatabaseMode:
        return DatabaseMode.SQLITE

    @property
    def is_postgres(self) -> bool:
        return False

    def _set_mode(self, mode: DatabaseMode, reason: str) -> None:
        self._switch_reason = reason

    def _sqlite_has_schema(self) -> bool:
        if not os.path.isfile(_sqlite_path()):
            return False
        try:
            with sqlite3.connect(_sqlite_path()) as con:
                return con.execute("PRAGMA quick_check").fetchone()[0] == "ok" and {
                    "users", "cards", "user_cards", "desired_cards"
                }.issubset({row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        except sqlite3.Error:
            return False

    @contextmanager
    def get_sqlite_connection(self) -> Iterator[sqlite3.Connection]:
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
        setup_sqlite_functions(con)
        changes_before = con.total_changes
        try:
            yield con
            con.commit()
            if con.total_changes > changes_before:
                self.mark_dirty()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get_status(self) -> dict:
        return {
            "mode": "sqlite",
            "pg_available": bool(_pg_url()),
            "switch_reason": self._switch_reason,
            "last_sync_at": self._last_sync_at,
            "sync_error_count": self._sync_error_count,
            "sqlite_path": _sqlite_path(),
        }

    def notify_pg_write_failed(self, exc: Exception) -> None:
        logger.warning("[DB] PostgreSQL недоступен; локальные данные сохранены: %s", exc)



def get_manager() -> DatabaseManager:
    return DatabaseManager.get_instance()

=======

>>>>>>> REMOTE (Changes from GitHub)

=======
"""Local SQLite connection manager with optional PostgreSQL snapshots."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

logger = logging.getLogger(__name__)
_sqlite_schema_lock = threading.Lock()
_sqlite_schema_paths: set[str] = set()


def _sqlite_path() -> str:
    path = os.getenv("SQLITE_DATABASE_PATH", "").strip()
    if path:
        return path
    if os.path.exists("/home/container/data") or os.path.exists("/home/container"):
        return "/home/container/data/pubg_local.db"
    return os.path.join(".", "data", "pubg_local.db")


def _pg_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _connect_timeout() -> float:
    return max(1.0, float(os.getenv("DB_FAILOVER_CONNECT_TIMEOUT", "5")))


class DatabaseMode(Enum):
    SQLITE = "sqlite"


class DatabaseManager:
    """SQLite is authoritative; PostgreSQL receives delayed full snapshots."""

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
        self._mode = DatabaseMode.SQLITE
        self._mode_lock = threading.RLock()
        self._initialized = False
        self._dirty = threading.Event()
        self._stop_backup = threading.Event()
        self._backup_thread: threading.Thread | None = None
        self._last_sync_at: float | None = None
        self._sync_error_count = 0
        self._switch_reason = "Локальная SQLite — основная база"

    def initialize(self) -> None:
        from db_sqlite_schema import ensure_sqlite_schema

        sqlite_db = _sqlite_path()
        existed = os.path.isfile(sqlite_db) and os.path.getsize(sqlite_db) > 0
        logger.info("Инициализация локальной SQLite: %s", sqlite_db)
        try:
            ensure_sqlite_schema(sqlite_db)
        except (sqlite3.DatabaseError, OSError) as exc:
            raise RuntimeError(f"Локальная SQLite недоступна или повреждена ({sqlite_db}): {exc}") from exc

        self._initialized = True
        if not existed:
            if not _pg_url():
                logger.info("[DB] PostgreSQL не настроен, работаем только локально")
                logger.warning("[DB] локальная БД отсутствует, PostgreSQL недоступен (причина: DATABASE_URL не задан)")
                logger.warning("[DB] создана пустая локальная БД, требуется ручное восстановление")
            else:
                self.restore_from_pg_if_missing(force=True)

        self._start_backup_thread()

    def restore_from_pg_if_missing(self, force: bool = False) -> int:
        sqlite_db = _sqlite_path()
        if not force and os.path.isfile(sqlite_db) and os.path.getsize(sqlite_db) > 0:
            return 0
        try:
            from db_sync import restore_snapshot_from_pg
            restored = restore_snapshot_from_pg(sqlite_db)
            if restored < 1:
                raise RuntimeError("снапшот PostgreSQL пуст")
            logger.info("[DB] локальная БД восстановлена из PostgreSQL (%s строк)", restored)
            return restored
        except Exception as exc:
            logger.warning("[DB] локальная БД отсутствует, PostgreSQL недоступен (причина: %s)", exc)
            logger.warning("[DB] создана пустая локальная БД, требуется ручное восстановление")
            return 0

    def _start_backup_thread(self) -> None:
        if not _pg_url() or self._backup_thread and self._backup_thread.is_alive():
            return
        self._stop_backup.clear()
        self._backup_thread = threading.Thread(
            target=self._backup_loop, name="database-snapshot-backup", daemon=True
        )
        self._backup_thread.start()

    def _backup_loop(self) -> None:
        debounce_seconds = max(5.0, min(10.0, float(os.getenv("DB_BACKUP_DEBOUNCE", "7"))))
        while not self._stop_backup.is_set():
            if not self._dirty.wait(timeout=1):
                continue
            self._dirty.clear()
            deadline = time.monotonic() + debounce_seconds
            while not self._stop_backup.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self._dirty.wait(timeout=remaining):
                    self._dirty.clear()
                    deadline = time.monotonic() + debounce_seconds
            if self._stop_backup.is_set():
                return
            self._dirty.clear()
            try:
                from db_sync import backup_snapshot_to_pg
                rows = backup_snapshot_to_pg()
                self._last_sync_at = time.time()
                logger.info("[DB] PostgreSQL backup snapshot completed (%s строк)", rows)
            except Exception as exc:
                self._sync_error_count += 1
                logger.warning("[DB] ошибка резервного копирования PostgreSQL: %s", exc)
                self._dirty.set()
                self._stop_backup.wait(10)

    def mark_dirty(self) -> None:
        self._dirty.set()

    def stop_backup(self) -> None:
        self._stop_backup.set()
        if self._backup_thread and self._backup_thread.is_alive():
            self._backup_thread.join(timeout=2)

    @property
    def mode(self) -> DatabaseMode:
        return DatabaseMode.SQLITE

    @property
    def is_postgres(self) -> bool:
        return False

    def _set_mode(self, mode: DatabaseMode, reason: str) -> None:
        self._switch_reason = reason

    def _sqlite_has_schema(self) -> bool:
        if not os.path.isfile(_sqlite_path()):
            return False
        try:
            with sqlite3.connect(_sqlite_path()) as con:
                return con.execute("PRAGMA quick_check").fetchone()[0] == "ok" and {
                    "users", "cards", "user_cards", "desired_cards"
                }.issubset({row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        except sqlite3.Error:
            return False

    @contextmanager
    def get_sqlite_connection(self) -> Iterator[sqlite3.Connection]:
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
        setup_sqlite_functions(con)
        changes_before = con.total_changes
        try:
            yield con
            con.commit()
            if con.total_changes > changes_before:
                self.mark_dirty()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get_status(self) -> dict:
        return {
            "mode": "sqlite",
            "pg_available": bool(_pg_url()),
            "switch_reason": self._switch_reason,
            "last_sync_at": self._last_sync_at,
            "sync_error_count": self._sync_error_count,
            "sqlite_path": _sqlite_path(),
        }

    def notify_pg_write_failed(self, exc: Exception) -> None:
        logger.warning("[DB] PostgreSQL недоступен; локальные данные сохранены: %s", exc)

    def invalidate_sqlite_snapshot(self, reason: str) -> None:
        logger.warning("Локальная SQLite остаётся источником истины: %s", reason[:200])


def get_manager() -> DatabaseManager:
    return DatabaseManager.get_instance()

>>>>>>> REMOTE (Changes from GitHub)
