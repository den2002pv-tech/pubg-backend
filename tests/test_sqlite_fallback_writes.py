import sqlite3

import pytest

import analytics
import database
from database_manager import DatabaseManager, DatabaseMode
from db_sqlite_schema import ensure_sqlite_schema, setup_sqlite_functions


@pytest.fixture
def sqlite_fallback(monkeypatch, tmp_path):
    db_path = tmp_path / "pubg_local.db"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", "sqlite-fallback-test-placeholder")
    previous_manager = DatabaseManager._instance
    DatabaseManager._instance = None
    manager = DatabaseManager.get_instance()
    manager._set_mode(DatabaseMode.SQLITE, "isolated SQLite fallback test")
    ensure_sqlite_schema(str(db_path))

    with sqlite3.connect(db_path) as con:
        setup_sqlite_functions(con, outbox_enabled=False)
        con.executemany(
            "INSERT INTO cards (id, name, rarity, icon) VALUES (?, ?, ?, ?)",
            [("card_1", "Card 1", 1, ""), ("card_2", "Card 2", 1, "")],
        )
        con.execute(
            "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
            (7001, "test"),
        )
        user_id = con.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (7001,)
        ).fetchone()[0]
        con.execute(
            "INSERT INTO desired_cards (user_id, card_id) VALUES (?, ?)",
            (user_id, "card_1"),
        )

    try:
        yield db_path
    finally:
        DatabaseManager._instance = previous_manager


def test_sqlite_fallback_saves_quantities_and_removes_owned_cards_from_wishlist(sqlite_fallback):
    database.set_user_card_quantities(7001, [
        (" card_1 ", 2),
        ("card_2", 0),
        ("card_1", 5),
    ])

    with sqlite3.connect(sqlite_fallback) as con:
        rows = con.execute(
            """SELECT uc.card_id, uc.quantity
               FROM user_cards uc JOIN users u ON u.id = uc.user_id
               WHERE u.telegram_id = ? ORDER BY uc.card_id""",
            (7001,),
        ).fetchall()
        wishlist = con.execute(
            """SELECT dc.card_id
               FROM desired_cards dc JOIN users u ON u.id = dc.user_id
               WHERE u.telegram_id = ?""",
            (7001,),
        ).fetchall()

    assert rows == [("card_1", 5), ("card_2", 0)]
    assert wishlist == []

    # Absolute updates stay idempotent when the client retries the same save.
    database.set_user_card_quantities(7001, [("card_1", 5)])
    with sqlite3.connect(sqlite_fallback) as con:
        assert con.execute(
            "SELECT quantity FROM user_cards WHERE card_id = 'card_1'"
        ).fetchone()[0] == 5


def test_sqlite_fallback_consumes_analytics_returning_rows(sqlite_fallback):
    assert analytics.record_analytics_events(1, [{
        "name": "tab_open",
        "properties": {"tab": "inventory"},
    }]) == 1

    with sqlite3.connect(sqlite_fallback) as con:
        row = con.execute(
            "SELECT event_name, properties FROM user_analytics_events"
        ).fetchone()

    assert row[0] == "tab_open"
    assert '"tab": "inventory"' in row[1]
