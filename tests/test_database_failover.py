import sqlite3
import os
import uuid
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
import asyncio

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
    sync = SyncManager(str(sqlite_path))
    with psycopg.connect(test_url) as pg:
        pg.execute("UPDATE user_cards SET quantity=4 WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'")
    # Simulate process death after the PG commit and before SQLite delivery.
    with sqlite3.connect(sqlite_path) as con:
        assert con.execute("SELECT quantity FROM user_cards WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'").fetchone()[0] == 2
    assert sync.sync_postgres_outbox() >= 1
    assert SyncManager(str(sqlite_path)).sync_postgres_outbox() == 0

    with psycopg.connect(test_url) as pg:
        last = pg.execute("SELECT transaction_id, transaction_sequence FROM app_outbox_transactions ORDER BY transaction_sequence DESC LIMIT 1").fetchone()
    with sqlite3.connect(sqlite_path) as con:
        con.execute("UPDATE database_metadata SET value=? WHERE key='pg_outbox_cursor'", (str(int(last[1]) - 1),))
    # Deliberately redeliver a transaction whose SQLite receipt was committed.
    assert sync.sync_postgres_outbox() == 1
    with sqlite3.connect(sqlite_path) as con:
        assert con.execute("SELECT COUNT(*) FROM replicated_outbox_transactions WHERE transaction_id=?", (last[0],)).fetchone()[0] == 1

    with psycopg.connect(test_url) as connection:
        row = connection.execute(
            """SELECT uc.quantity FROM user_cards uc JOIN users u ON u.id=uc.user_id
               WHERE u.telegram_id=%s AND uc.card_id=%s""",
            (7001, "card_1"),
        ).fetchone()
    assert row[0] == 4
    with sqlite3.connect(sqlite_path) as con:
        assert con.execute("SELECT quantity FROM user_cards WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'").fetchone()[0] == 4


def test_isolated_postgres_outbox_gap_is_detected_without_advancing_cursor(isolated_pg_and_sqlite):
    manager, sqlite_path, test_url = isolated_pg_and_sqlite
    with psycopg.connect(test_url) as connection:
        connection.execute("UPDATE user_cards SET quantity=3 WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'")
        tx = connection.execute("SELECT transaction_id, transaction_sequence FROM app_outbox_transactions ORDER BY transaction_sequence DESC LIMIT 1").fetchone()
        connection.execute("DELETE FROM app_outbox_events WHERE transaction_id=%s", (tx[0],))
    sync = SyncManager(str(sqlite_path))
    with pytest.raises(RuntimeError, match="missing or reordered events"):
        sync.sync_postgres_outbox()
    with sqlite3.connect(sqlite_path) as con:
        cursor = int(con.execute("SELECT value FROM database_metadata WHERE key='pg_outbox_cursor'").fetchone()[0])
    assert cursor < int(tx[1])


def test_postgres_outbox_rolls_back_with_business_write_and_sequences_concurrent_transactions(isolated_pg_and_sqlite):
    _manager, _sqlite_path, test_url = isolated_pg_and_sqlite
    with psycopg.connect(test_url) as connection:
        start = connection.execute("SELECT last_sequence FROM app_outbox_clock WHERE singleton").fetchone()[0]
        try:
            with connection.transaction():
                connection.execute("UPDATE cards SET name='rolled back' WHERE id='card_1'")
                raise ValueError("force rollback")
        except ValueError:
            pass
        assert connection.execute("SELECT last_sequence FROM app_outbox_clock WHERE singleton").fetchone()[0] == start

    def write_event(index):
        with psycopg.connect(test_url) as connection:
            connection.execute(
                "INSERT INTO user_analytics_events (user_id, event_name, properties) VALUES ((SELECT id FROM users WHERE telegram_id=7001), %s, '{}'::jsonb)",
                (f"parallel_{index}",),
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_event, range(8)))
    with psycopg.connect(test_url) as connection:
        sequences = connection.execute(
            "SELECT transaction_sequence, event_count FROM app_outbox_transactions ORDER BY transaction_sequence"
        ).fetchall()
        assert [int(row[0]) for row in sequences] == list(range(1, len(sequences) + 1))
        assert all(int(row[1]) >= 1 for row in sequences)


def test_caught_nested_database_error_rolls_back_its_savepoint(isolated_pg_and_sqlite):
    _manager, _sqlite_path, test_url = isolated_pg_and_sqlite
    import database

    with psycopg.connect(test_url) as pg:
        before_clock = pg.execute("SELECT last_sequence FROM app_outbox_clock WHERE singleton").fetchone()[0]
    with database.get_connection():
        try:
            with database.get_connection() as nested:
                nested.execute(
                    "UPDATE user_cards SET quantity=9 WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'"
                )
                nested.commit()
                raise ValueError("caller handled expected business rejection")
        except ValueError:
            pass
    with psycopg.connect(test_url) as pg:
        assert pg.execute("SELECT quantity FROM user_cards WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'").fetchone()[0] == 2
        assert pg.execute("SELECT last_sequence FROM app_outbox_clock WHERE singleton").fetchone()[0] == before_clock


def test_telegram_update_marker_and_business_write_rollback_and_deduplicate(isolated_pg_and_sqlite, monkeypatch):
    _manager, _sqlite_path, test_url = isolated_pg_and_sqlite
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret")
    calls = []

    def fail_after_write(_message):
        import database
        database.set_user_card_quantity(7001, "card_1", 8)
        calls.append("failed")
        raise RuntimeError("injected handler failure")

    monkeypatch.setattr(trade_requests, "_handle_webhook_message", fail_after_write)
    payload = {"update_id": 992001, "message": {"text": "/start", "chat": {"id": 7001}}}
    with pytest.raises(RuntimeError, match="injected handler failure"):
        asyncio.run(trade_requests.telegram_webhook(payload, "test-secret"))
    with psycopg.connect(test_url) as connection:
        assert connection.execute("SELECT 1 FROM processed_telegram_updates WHERE update_id=992001").fetchone() is None
        assert connection.execute("SELECT quantity FROM user_cards WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'").fetchone()[0] == 2

    def succeed(_message):
        import database
        calls.append("ok")
        database.set_user_card_quantity(7001, "card_1", 8)

    monkeypatch.setattr(trade_requests, "_handle_webhook_message", succeed)
    asyncio.run(trade_requests.telegram_webhook(payload, "test-secret"))
    asyncio.run(trade_requests.telegram_webhook(payload, "test-secret"))
    assert calls == ["failed", "ok"]
    with psycopg.connect(test_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM processed_telegram_updates WHERE update_id=992001").fetchone()[0] == 1


def test_sqlite_apply_failure_preserves_cursor_and_retries_after_restart(isolated_pg_and_sqlite):
    _manager, sqlite_path, test_url = isolated_pg_and_sqlite
    with psycopg.connect(test_url) as pg:
        pg.execute("UPDATE user_cards SET quantity=5 WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'")
    with sqlite3.connect(sqlite_path) as con:
        cursor = con.execute("SELECT value FROM database_metadata WHERE key='pg_outbox_cursor'").fetchone()[0]
        con.execute("CREATE TRIGGER reject_replica_update BEFORE UPDATE ON user_cards BEGIN SELECT RAISE(ABORT, 'replica unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match="replica unavailable"):
        SyncManager(str(sqlite_path)).sync_postgres_outbox()
    with sqlite3.connect(sqlite_path) as con:
        assert con.execute("SELECT value FROM database_metadata WHERE key='pg_outbox_cursor'").fetchone()[0] == cursor
        con.execute("DROP TRIGGER reject_replica_update")
    assert SyncManager(str(sqlite_path)).sync_postgres_outbox() == 1
    with sqlite3.connect(sqlite_path) as con:
        assert con.execute("SELECT quantity FROM user_cards WHERE user_id=(SELECT id FROM users WHERE telegram_id=7001) AND card_id='card_1'").fetchone()[0] == 5


def test_trade_completion_and_outbox_rollback_together_on_transfer_failure(isolated_pg_and_sqlite):
    _manager, _sqlite_path, test_url = isolated_pg_and_sqlite
    request = trade_requests._create_request_record(7001, 7002, "card_1", "card_2")
    request_id = int(request["id"])
    trade_requests._transition(request_id, 7002, "accept")
    trade_requests._transition(request_id, 7001, "agree")
    with psycopg.connect(test_url) as pg:
        pg.execute("UPDATE trade_requests SET exchange_code_sent_at=NOW() WHERE id=%s", (request_id,))
        before_clock = pg.execute("SELECT last_sequence FROM app_outbox_clock WHERE singleton").fetchone()[0]
        pg.execute("""CREATE FUNCTION reject_trade_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF OLD.user_id=(SELECT id FROM users WHERE telegram_id=7002)
                 AND OLD.card_id='card_2' AND NEW.quantity=OLD.quantity-1 THEN
                RAISE EXCEPTION 'injected transfer failure';
              END IF;
              RETURN NEW;
            END $$""")
        pg.execute("CREATE TRIGGER reject_trade_transfer BEFORE UPDATE ON user_cards FOR EACH ROW EXECUTE FUNCTION reject_trade_transfer()")
    trade_requests._transition(request_id, 7001, "complete")
    with pytest.raises(psycopg.Error, match="injected transfer failure"):
        trade_requests._transition(request_id, 7002, "complete")
    with psycopg.connect(test_url) as pg:
        assert pg.execute("SELECT status FROM trade_requests WHERE id=%s", (request_id,)).fetchone()[0] == "agreed"
        assert pg.execute("SELECT last_sequence FROM app_outbox_clock WHERE singleton").fetchone()[0] == before_clock + 1
        pg.execute("DROP TRIGGER reject_trade_transfer ON user_cards")
        pg.execute("DROP FUNCTION reject_trade_transfer()")


def test_isolated_postgres_trade_transfer_replicates_as_one_transaction(isolated_pg_and_sqlite):
    manager, sqlite_path, test_url = isolated_pg_and_sqlite
    request = trade_requests._create_request_record(7001, 7002, "card_1", "card_2")
    request_id = int(request["id"])
    trade_requests._transition(request_id, 7002, "accept")
    trade_requests._transition(request_id, 7001, "agree")
    with psycopg.connect(test_url) as connection:
        connection.execute("UPDATE trade_requests SET exchange_code_sent_at=NOW() WHERE id=%s", (request_id,))
    trade_requests._transition(request_id, 7001, "complete")
    SyncManager(str(sqlite_path)).sync_postgres_outbox()
    trade_requests._transition(request_id, 7002, "complete")
    with psycopg.connect(test_url) as connection:
        committed = connection.execute("SELECT status FROM trade_requests WHERE id=%s", (request_id,)).fetchone()[0]
        latest = connection.execute("SELECT transaction_id, transaction_sequence, event_count FROM app_outbox_transactions ORDER BY transaction_sequence DESC LIMIT 1").fetchone()
    assert committed == "completed"
    assert latest[2] >= 5  # status plus both users' inventory/wishlist mutations
    sync = SyncManager(str(sqlite_path))
    assert sync.sync_postgres_outbox() >= 1
    assert sync.sync_postgres_outbox() == 0
    with sqlite3.connect(sqlite_path) as con:
        assert con.execute("SELECT status FROM trade_requests WHERE id=?", (request_id,)).fetchone()[0] == "completed"


def test_startup_creates_empty_local_sqlite_without_postgres(monkeypatch, tmp_path):
    manager, _ = _fresh_manager(monkeypatch, tmp_path)
    manager.initialize()
    assert manager.mode is DatabaseMode.SQLITE
    assert manager._sqlite_has_schema()


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
    with sqlite3.connect(db_path) as con:
        assert con.execute("SELECT name FROM cards WHERE id='card_1'").fetchone()[0] == "Card 1"


def test_postgres_is_not_used_for_request_connections(monkeypatch, tmp_path):
    manager, _db_path = _fresh_manager(monkeypatch, tmp_path, database_url="postgresql://unused")
    import database

    def unavailable(*_args, **_kwargs):
        raise psycopg.OperationalError("test PostgreSQL is unavailable")

    monkeypatch.setattr(database.psycopg, "connect", unavailable)
    manager.initialize()
    with database.get_connection() as conn:
        conn.execute("SELECT 1").fetchone()
    assert manager.mode is DatabaseMode.SQLITE


def test_replicated_tables_reject_truncate_bypass(isolated_pg_and_sqlite):
    _manager, _sqlite_path, test_url = isolated_pg_and_sqlite
    with pytest.raises(psycopg.Error, match="TRUNCATE is disabled"):
        with psycopg.connect(test_url) as pg:
            pg.execute("TRUNCATE user_cards")


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
        with pytest.raises(RuntimeError, match="divergent row"):
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


def test_sqlite_schema_does_not_install_sync_outbox_triggers(tmp_path):
    from db_sqlite_schema import setup_sqlite_functions

    db_path = tmp_path / "transaction-ids.sqlite3"
    ensure_sqlite_schema(str(db_path))
    con = sqlite3.connect(db_path)
    setup_sqlite_functions(con, outbox_enabled=True)
    con.execute("INSERT INTO cards (id, name) VALUES ('card_1', 'Card 1')")
    con.commit()
    con.execute("UPDATE cards SET name='Card one' WHERE id='card_1'")
    con.commit()
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_outbox_%'").fetchall()
    con.close()
    assert rows == []


def test_sync_pending_outbox_is_disabled_in_postgres_primary_mode():
    with pytest.raises(RuntimeError, match="SQLite→PostgreSQL replay отключён"):
        SyncManager().sync_pending_outbox()

