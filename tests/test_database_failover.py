import sqlite3
import threading

import pytest

from database_manager import DatabaseManager, DatabaseMode
from db_sqlite_schema import ensure_sqlite_schema


@pytest.fixture(autouse=True)
def reset_manager():
    previous = DatabaseManager._instance
    DatabaseManager._instance = None
    yield
    if DatabaseManager._instance:
        DatabaseManager._instance.stop_backup()
    DatabaseManager._instance = previous


def _configure(monkeypatch, tmp_path, database_url=""):
    path = tmp_path / "pubg_local.db"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", database_url)
    DatabaseManager._instance = None
    return DatabaseManager.get_instance(), path


def test_startup_without_postgres_creates_local_database(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    manager, db_path = _configure(monkeypatch, tmp_path)

    manager.initialize()

    assert manager.mode is DatabaseMode.SQLITE
    assert manager._sqlite_has_schema()
    assert "PostgreSQL не настроен, работаем только локально" in caplog.text
    assert "требуется ручное восстановление" in caplog.text


def test_existing_local_database_is_not_restored(monkeypatch, tmp_path):
    manager, db_path = _configure(monkeypatch, tmp_path, "postgresql://unused")
    ensure_sqlite_schema(str(db_path))
    with sqlite3.connect(db_path) as con:
        con.execute("INSERT INTO cards (id, name) VALUES ('card_local', 'Local')")

    def unexpected_restore(*_args, **_kwargs):
        pytest.fail("existing local database must not be restored")

    import db_sync
    monkeypatch.setattr(db_sync, "restore_snapshot_from_pg", unexpected_restore)
    manager.initialize()

    with sqlite3.connect(db_path) as con:
        assert con.execute("SELECT name FROM cards WHERE id='card_local'").fetchone()[0] == "Local"


def test_missing_local_database_survives_unavailable_postgres(monkeypatch, tmp_path, caplog):
    manager, db_path = _configure(monkeypatch, tmp_path, "postgresql://unavailable")

    def unavailable(*_args, **_kwargs):
        raise ConnectionError("connection refused")

    import db_sync
    monkeypatch.setattr(db_sync, "restore_snapshot_from_pg", unavailable)
    manager.initialize()

    assert manager._sqlite_has_schema()
    assert "PostgreSQL недоступен (причина: connection refused)" in caplog.text
    assert "создана пустая локальная БД" in caplog.text


def test_missing_local_database_restores_postgres_snapshot(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    manager, db_path = _configure(monkeypatch, tmp_path, "postgresql://configured")

    def restore(path):
        with sqlite3.connect(path) as con:
            con.execute("INSERT INTO cards (id, name) VALUES ('card_pg', 'Restored')")
        return 1

    import db_sync
    monkeypatch.setattr(db_sync, "restore_snapshot_from_pg", restore)
    manager.initialize()

    with sqlite3.connect(db_path) as con:
        assert con.execute("SELECT name FROM cards WHERE id='card_pg'").fetchone()[0] == "Restored"
    assert "локальная БД восстановлена из PostgreSQL (1 строк)" in caplog.text


def test_successful_commit_marks_database_dirty(monkeypatch, tmp_path):
    manager, _db_path = _configure(monkeypatch, tmp_path)
    manager.initialize()

    import database
    with database.get_connection() as connection:
        connection.execute("INSERT INTO cards (id, name) VALUES (?, ?)", ("card_1", "Card 1"))
        connection.commit()

    assert manager._dirty.is_set()


def test_committed_write_stays_dirty_if_later_code_fails(monkeypatch, tmp_path):
    manager, _db_path = _configure(monkeypatch, tmp_path)
    manager.initialize()

    import database
    with pytest.raises(RuntimeError, match="after commit"):
        with database.get_connection() as connection:
            connection.execute("INSERT INTO cards (id, name) VALUES (?, ?)", ("card_1", "Card 1"))
            connection.commit()
            raise RuntimeError("after commit")

    assert manager._dirty.is_set()


def test_rollback_does_not_mark_database_dirty(monkeypatch, tmp_path):
    manager, _db_path = _configure(monkeypatch, tmp_path)
    manager.initialize()

    import database
    with pytest.raises(RuntimeError, match="rollback"):
        with database.get_connection() as connection:
            connection.execute("INSERT INTO cards (id, name) VALUES (?, ?)", ("card_1", "Card 1"))
            raise RuntimeError("rollback")

    assert not manager._dirty.is_set()


def test_dirty_snapshot_is_sent_after_debounce(monkeypatch, tmp_path):
    manager, _db_path = _configure(monkeypatch, tmp_path, "postgresql://configured")
    completed = threading.Event()
    calls = []

    def backup():
        calls.append(True)
        completed.set()
        return 1

    import db_sync
    monkeypatch.setattr(db_sync, "backup_snapshot_to_pg", backup)
    manager.initialize()
    manager.mark_dirty()

    assert completed.wait(timeout=9)
    assert calls == [True]


def test_sqlite_schema_has_no_outbox_tables_or_triggers(tmp_path):
    db_path = tmp_path / "clean.sqlite3"
    ensure_sqlite_schema(str(db_path))

    with sqlite3.connect(db_path) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}

    assert not tables.intersection({
        "outbox_operations", "sync_lock", "outbox_applied_in_pg", "replicated_outbox_transactions"
    })
    assert not any(name.startswith("trg_outbox_") for name in triggers)
