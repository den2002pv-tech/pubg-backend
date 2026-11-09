import sqlite3
import os
import uuid
import multiprocessing

import psycopg
import pytest

from database_manager import DatabaseManager, DatabaseMode
from db_sqlite_schema import ensure_sqlite_schema
from db_sync import OutboxManager, SyncManager
import trade_requests


def _hold_sync_lock(db_path, ready_fd, release_fd):
    sync = SyncManager(db_path)
    os.write(ready_fd, b"1" if sync._try_acquire_sync_lock() else b"0")
    os.close(ready_fd)
    os.read(release_fd, 1)
    os.close(release_fd)
    sync._release_sync_lock()


@pytest.fixture(autouse=True)
def reset_database_manager_singleton():
    previous = DatabaseManager._instance
    DatabaseManager._instance = None
    yield
    DatabaseManager._instance = previous


def _fresh_manager(monkeypatch, tmp_path, database_url=""):
    db_path = tmp_path / "persistent" / "cards.sqlite3"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", database_url)
    DatabaseManager._instance = None
    return DatabaseManager.get_instance(), db_path


@pytest.fixture
def isolated_pg_and_sqlite(monkeypatch, tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for isolated PostgreSQL failover tests")
    schema = "test_failover_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    test_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
    monkeypatch.setenv("DATABASE_URL", test_url)
    sqlite_path = tmp_path / "failover.sqlite3"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(sqlite_path))
    manager = DatabaseManager.get_instance()
    manager._set_mode(DatabaseMode.POSTGRES, "isolated PostgreSQL test")
    ensure_sqlite_schema(str(sqlite_path))

    import analytics
    import database
    database.init_db()
    analytics.init_analytics_db()
    trade_requests.init_trade_request_schema()
    database.sync_cards({"card_1": {"name": "Card 1"}, "card_2": {"name": "Card 2"}})
    database.get_or_create_user(7001, first_name="Test")
    database.get_or_create_user(7002, first_name="Trade target")
    database.set_user_card_quantity(7001, "card_1", 2)
    database.set_user_card_quantity(7002, "card_2", 2)
    database.set_desired_cards(7002, ["card_1"])
    sync = SyncManager(str(sqlite_path))
    sync.initial_sync_pg_to_sqlite()
    manager._initialized = True
    yield manager, sqlite_path, test_url

    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


def test_isolated_postgres_syncs_exact_sqlite_row_events_idempotently(isolated_pg_and_sqlite):
    manager, sqlite_path, test_url = isolated_pg_and_sqlite
    import database

    manager._set_mode(DatabaseMode.SQLITE, "simulate outage")
    database.set_user_card_quantity(7001, "card_1", 4)
    sync = SyncManager(str(sqlite_path))
    mark_synced = sync._outbox.mark_synced
    sync._outbox.mark_synced = lambda _op_id: (_ for _ in ()).throw(RuntimeError("injected ack failure"))
    with pytest.raises(RuntimeError, match="не подтверждены"):
        sync.sync_pending_outbox()
    sync._outbox.mark_synced = mark_synced
    with sqlite3.connect(sqlite_path) as con:
        con.create_function("database_outbox_enabled", 0, lambda: 0)
        con.execute(
            "UPDATE outbox_operations SET last_attempt_at='2000-01-01T00:00:00Z' WHERE sync_status='failed'"
        )
    sync.sync_pending_outbox()
    sync.sync_pending_outbox()

    with psycopg.connect(test_url) as connection:
        row = connection.execute(
            """SELECT uc.quantity FROM user_cards uc JOIN users u ON u.id=uc.user_id
               WHERE u.telegram_id=%s AND uc.card_id=%s""",
            (7001, "card_1"),
        ).fetchone()
    assert row[0] == 4
    assert sync.verify_integrity() is True


def test_isolated_postgres_conflict_blocks_failback(isolated_pg_and_sqlite):
    manager, sqlite_path, test_url = isolated_pg_and_sqlite
    import database

    manager._set_mode(DatabaseMode.SQLITE, "simulate outage")
    database.set_user_card_quantity(7001, "card_1", 4)
    with psycopg.connect(test_url) as connection:
        connection.execute(
            """UPDATE user_cards SET quantity=7
               WHERE user_id=(SELECT id FROM users WHERE telegram_id=%s) AND card_id=%s""",
            (7001, "card_1"),
        )
        connection.commit()

    manager._run_health_cycle()

    assert manager.mode is DatabaseMode.SQLITE
    assert SyncManager(str(sqlite_path)).verify_integrity() is False


def test_isolated_sqlite_trade_transfer_replays_as_one_pg_transaction(isolated_pg_and_sqlite):
    manager, sqlite_path, test_url = isolated_pg_and_sqlite
    manager._set_mode(DatabaseMode.SQLITE, "simulate outage")

    request = trade_requests._create_request_record(7001, 7002, "card_1", "card_2")
    request_id = int(request["id"])
    trade_requests._transition(request_id, 7002, "accept")
    trade_requests._transition(request_id, 7001, "agree")
    with manager.get_sqlite_connection() as con:
        con.execute("UPDATE trade_requests SET exchange_code_sent_at=now_utc() WHERE id=?", (request_id,))
    trade_requests._transition(request_id, 7001, "complete")
    trade_requests._transition(request_id, 7002, "complete")

    with psycopg.connect(test_url) as connection:
        connection.execute(
            """CREATE FUNCTION reject_second_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                   IF OLD.user_id=(SELECT id FROM users WHERE telegram_id=7002)
                      AND OLD.card_id='card_2' AND NEW.quantity=OLD.quantity-1 THEN
                       RAISE EXCEPTION 'injected PostgreSQL transfer failure';
                   END IF;
                   RETURN NEW;
               END $$"""
        )
        connection.execute(
            "CREATE TRIGGER reject_second_transfer BEFORE UPDATE ON user_cards "
            "FOR EACH ROW EXECUTE FUNCTION reject_second_transfer()"
        )
        connection.commit()

    sync = SyncManager(str(sqlite_path))
    with pytest.raises(RuntimeError, match="не подтверждены"):
        sync.sync_pending_outbox()
    with psycopg.connect(test_url) as connection:
        row = connection.execute(
            "SELECT status FROM trade_requests WHERE id=%s", (request_id,)
        ).fetchone()
        inventories = connection.execute(
            """SELECT u.telegram_id, uc.card_id, uc.quantity FROM user_cards uc
               JOIN users u ON u.id=uc.user_id WHERE u.telegram_id IN (7001,7002)
               ORDER BY u.telegram_id, uc.card_id"""
        ).fetchall()
        connection.execute("DROP TRIGGER reject_second_transfer ON user_cards")
        connection.execute("DROP FUNCTION reject_second_transfer()")
        connection.commit()
    assert row[0] == "agreed"
    assert [(r[0], r[1], r[2]) for r in inventories] == [
        (7001, "card_1", 2), (7002, "card_2", 2)
    ]

    with sqlite3.connect(sqlite_path) as con:
        con.create_function("database_outbox_enabled", 0, lambda: 0)
        con.execute(
            "UPDATE outbox_operations SET last_attempt_at='2000-01-01T00:00:00Z' WHERE sync_status='failed'"
        )
    sync.sync_pending_outbox()
    assert sync.verify_integrity() is True


def test_startup_refuses_empty_sqlite_without_postgres(monkeypatch, tmp_path):
    manager, _ = _fresh_manager(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="локальная SQLite не содержит"):
        manager.initialize()


def test_startup_uses_existing_sqlite_when_postgres_url_missing(monkeypatch, tmp_path):
    manager, db_path = _fresh_manager(monkeypatch, tmp_path)
    ensure_sqlite_schema(str(db_path))
    with sqlite3.connect(db_path) as con:
        con.create_function("database_outbox_enabled", 0, lambda: 0)
        con.create_function("database_transaction_id", 0, lambda: "test-transaction")
        con.execute("INSERT INTO cards (id, name) VALUES ('card_1', 'Card 1')")
        con.execute(
            "INSERT INTO database_metadata (key, value) VALUES ('postgres_snapshot_complete', 'test-snapshot')"
        )

    manager.initialize()

    assert manager.mode is DatabaseMode.SQLITE
    assert manager.get_status()["sqlite_path"] == str(db_path)


def test_sqlite_connection_creates_configured_parent(monkeypatch, tmp_path):
    manager, db_path = _fresh_manager(monkeypatch, tmp_path)

    with manager.get_sqlite_connection() as con:
        con.execute("SELECT 1")

    assert db_path.exists()


def test_startup_rejects_corrupt_sqlite(monkeypatch, tmp_path):
    manager, db_path = _fresh_manager(monkeypatch, tmp_path)
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"not a sqlite database")

    with pytest.raises(RuntimeError, match="повреждена"):
        manager.initialize()


def test_outbox_keeps_order_and_delays_failed_retries(monkeypatch, tmp_path):
    db_path = tmp_path / "outbox.sqlite3"
    ensure_sqlite_schema(str(db_path))
    monkeypatch.setenv("DB_SYNC_RETRY_DELAY", "3600")
    outbox = OutboxManager(str(db_path))

    first = outbox.add_operation("set_user_card_quantity", {"telegram_id": 1, "card_id": "card_1", "quantity": 2})
    second = outbox.add_operation("set_user_card_quantity", {"telegram_id": 1, "card_id": "card_2", "quantity": 1})
    assert [item["id"] for item in outbox.get_pending()] == [first, second]

    outbox.mark_failed(first, "temporary failure")
    pending = outbox.get_pending()
    assert [item["id"] for item in pending] == [second]


def test_initial_snapshot_does_not_replace_local_rows(tmp_path):
    db_path = tmp_path / "snapshot.sqlite3"
    ensure_sqlite_schema(str(db_path))
    with sqlite3.connect(db_path) as con:
        con.create_function("database_outbox_enabled", 0, lambda: 0)
        con.create_function("database_transaction_id", 0, lambda: "test-transaction")
        con.create_function("database_transaction_id", 0, lambda: "test-transaction")
        con.execute("INSERT INTO cards (id, name, rarity, icon) VALUES ('card_1', 'Local', 2, 'local.webp')")
        con.create_function("database_outbox_enabled", 0, lambda: 1)
        con.execute("UPDATE cards SET name='Pending local edit' WHERE id='card_1'")

    class FakePostgres:
        def execute(self, _query):
            pass

        def fetchall(self):
            return [{"id": "card_1", "name": "Remote", "rarity": 5, "icon": "remote.webp"}]

    sync = SyncManager(str(db_path))
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        con.create_function("database_outbox_enabled", 0, lambda: 0)
        sync._sync_table_pg_to_sqlite(
            FakePostgres(), con, "cards", pk="id", columns=["id", "name", "rarity", "icon"]
        )
        row = con.execute("SELECT name, rarity, icon FROM cards WHERE id='card_1'").fetchone()

    assert tuple(row) == ("Pending local edit", 2, "local.webp")


def test_unknown_outbox_operations_fail_instead_of_being_acknowledged():
    sync = SyncManager()
    with pytest.raises(ValueError, match="Unknown outbox operation"):
        sync._apply_operation(object(), "unknown_operation", {})


def test_sync_lock_is_exclusive_between_workers(tmp_path):
    db_path = tmp_path / "locked.sqlite3"
    ensure_sqlite_schema(str(db_path))
    sync = SyncManager(str(db_path))

    assert sync._try_acquire_sync_lock() is True
    assert sync._try_acquire_sync_lock() is False
    sync._release_sync_lock()
    assert sync._try_acquire_sync_lock() is True
    sync._release_sync_lock()


def test_sync_lock_is_exclusive_across_processes(tmp_path):
    db_path = tmp_path / "multiprocess-lock.sqlite3"
    ensure_sqlite_schema(str(db_path))
    ctx = multiprocessing.get_context("fork")
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    process = ctx.Process(
        target=_hold_sync_lock,
        args=(str(db_path), ready_write, release_read),
    )
    process.start()
    os.close(ready_write)
    os.close(release_read)
    try:
        assert os.read(ready_read, 1) == b"1"
        os.close(ready_read)
        assert SyncManager(str(db_path))._try_acquire_sync_lock() is False
    finally:
        os.write(release_write, b"x")
        os.close(release_write)
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0
    sync = SyncManager(str(db_path))
    assert sync._try_acquire_sync_lock() is True
    sync._release_sync_lock()


def test_sqlite_trade_completion_is_atomic_and_row_changes_are_journaled(monkeypatch, tmp_path):
    manager, db_path = _fresh_manager(monkeypatch, tmp_path)
    ensure_sqlite_schema(str(db_path))
    with sqlite3.connect(db_path) as con:
        con.create_function("database_outbox_enabled", 0, lambda: 0)
        con.create_function("database_transaction_id", 0, lambda: "seed-transaction")
        con.executemany(
            "INSERT INTO users (id, telegram_id, username, first_name, last_name) VALUES (?, ?, '', '', '')",
            [(101, 1001), (102, 1002)],
        )
        con.executemany(
            "INSERT INTO cards (id, name, rarity, icon) VALUES (?, ?, 1, '')",
            [("card_1", "Card 1"), ("card_2", "Card 2")],
        )
        con.executemany(
            "INSERT INTO user_cards (user_id, card_id, quantity) VALUES (?, ?, ?)",
            [(101, "card_1", 2), (102, "card_2", 2)],
        )
        con.execute("INSERT INTO desired_cards (user_id, card_id) VALUES (102, 'card_1')")
        con.execute(
            "INSERT INTO database_metadata (key, value) VALUES ('postgres_snapshot_complete', 'test-snapshot')"
        )
    manager.initialize()

    request = trade_requests._create_request_record(1001, 1002, "card_1", "card_2")
    request_id = int(request["id"])
    trade_requests._transition(request_id, 1002, "accept")
    trade_requests._transition(request_id, 1001, "agree")
    with manager.get_sqlite_connection() as con:
        con.execute("UPDATE trade_requests SET exchange_code_sent_at=now_utc() WHERE id=?", (request_id,))
    trade_requests._transition(request_id, 1001, "complete")
    with manager.get_sqlite_connection() as con:
        con.execute(
            """CREATE TRIGGER fail_second_card_transfer BEFORE UPDATE ON user_cards
               WHEN OLD.user_id=102 AND OLD.card_id='card_2'
               BEGIN SELECT RAISE(ABORT, 'injected transfer failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected transfer failure"):
        trade_requests._transition(request_id, 1002, "complete")

    with manager.get_sqlite_connection() as con:
        assert con.execute("SELECT status FROM trade_requests WHERE id=?", (request_id,)).fetchone()[0] == "agreed"
        assert con.execute("SELECT quantity FROM user_cards WHERE user_id=101 AND card_id='card_1'").fetchone()[0] == 2
        assert con.execute("SELECT quantity FROM user_cards WHERE user_id=102 AND card_id='card_2'").fetchone()[0] == 2
        con.execute("DROP TRIGGER fail_second_card_transfer")

    trade_requests._transition(request_id, 1002, "complete")
    with manager.get_sqlite_connection() as con:
        assert con.execute("SELECT status FROM trade_requests WHERE id=?", (request_id,)).fetchone()[0] == "completed"
        quantities = {
            (row["user_id"], row["card_id"]): row["quantity"]
            for row in con.execute("SELECT user_id, card_id, quantity FROM user_cards")
        }
        assert quantities == {
            (101, "card_1"): 1,
            (101, "card_2"): 1,
            (102, "card_1"): 1,
            (102, "card_2"): 1,
        }
        outbox = con.execute(
            "SELECT COUNT(*) FROM outbox_operations WHERE operation_type='sqlite_row_change' AND sync_status='pending'"
        ).fetchone()[0]
        assert outbox > 0

