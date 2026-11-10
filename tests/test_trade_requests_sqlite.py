import asyncio
import sqlite3

import pytest

import trade_requests as trades
from database_manager import DatabaseManager
from db_sqlite_schema import ensure_sqlite_schema, setup_sqlite_functions


def _seed(db_path):
    with sqlite3.connect(db_path) as con:
        setup_sqlite_functions(con)
        con.executemany(
            "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
            [(100, "sender"), (200, "recipient")],
        )
        con.executemany(
            "INSERT INTO cards(id,name,rarity,icon) VALUES(?,?,1,'')",
            [("card_1", "A"), ("card_2", "B")],
        )
        sender_id = con.execute("SELECT id FROM users WHERE telegram_id=100").fetchone()[0]
        recipient_id = con.execute("SELECT id FROM users WHERE telegram_id=200").fetchone()[0]
        con.execute(
            "INSERT INTO user_cards(user_id,card_id,quantity) VALUES(?,?,3)",
            (sender_id, "card_1"),
        )
        con.execute(
            "INSERT INTO user_cards(user_id,card_id,quantity) VALUES(?,?,3)",
            (recipient_id, "card_2"),
        )
        con.executemany(
            "INSERT INTO desired_cards(user_id,card_id) VALUES(?,?)",
            [(sender_id, "card_2"), (recipient_id, "card_1")],
        )


@pytest.fixture
def isolated_trade_db(monkeypatch, tmp_path):
    path = tmp_path / "trade.sqlite3"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", "")
    DatabaseManager._instance = None
    manager = DatabaseManager.get_instance()
    manager.initialize()
    ensure_sqlite_schema(str(path))
    _seed(path)
    try:
        yield path
    finally:
        manager.stop_backup()
        DatabaseManager._instance = None


def _prepare_agreed_request(path, monkeypatch):
    monkeypatch.setattr(trades, "_notify_request", lambda *_: True)
    monkeypatch.setattr(trades, "_telegram_call", lambda *_: {"ok": True})
    request = trades._create_request_record(100, 200, "card_1", "card_2")
    trades._transition(request["id"], 200, "accept")
    trades._transition(request["id"], 100, "agree")
    with sqlite3.connect(path) as con:
        con.execute(
            "UPDATE trade_requests SET exchange_code_sent_at='x' WHERE id=?",
            (request["id"],),
        )
    return request


def test_create_and_complete_transfers_cards_atomically(isolated_trade_db, monkeypatch):
    request = _prepare_agreed_request(isolated_trade_db, monkeypatch)
    assert trades._transition(request["id"], 100, "complete")["status"] == "agreed"
    result = trades._transition(request["id"], 200, "complete")
    assert result["status"] == "completed"

    with sqlite3.connect(isolated_trade_db) as con:
        quantities = dict(con.execute(
            "SELECT card_id, SUM(quantity) FROM user_cards GROUP BY card_id"
        ).fetchall())
        wanted_count = con.execute("SELECT COUNT(*) FROM desired_cards").fetchone()[0]
    assert quantities == {"card_1": 3, "card_2": 3}
    assert wanted_count == 0


def test_complete_returns_current_state_when_repeated(isolated_trade_db, monkeypatch):
    request = _prepare_agreed_request(isolated_trade_db, monkeypatch)
    trades._transition(request["id"], 100, "complete")
    trades._transition(request["id"], 200, "complete")
    second = trades._transition(request["id"], 100, "complete")
    assert second["status"] == "completed"


def test_webhook_dispatch_failure_can_be_retried(isolated_trade_db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")
    seen = []

    def fail(message):
        seen.append(message)
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(trades, "_handle_webhook_message", fail)
    payload = {
        "update_id": 999,
        "message": {
            "text": "/start",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
        },
    }
    for _ in range(2):
        with pytest.raises(RuntimeError, match="dispatch failed"):
            asyncio.run(trades.telegram_webhook(payload, secret="secret"))
    assert len(seen) == 2


def test_webhook_skips_replayed_update(isolated_trade_db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")
    sent = []
    monkeypatch.setattr(
        trades, "_telegram_call",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )
    payload = {
        "update_id": 42,
        "message": {
            "text": "/start",
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
        },
    }
    asyncio.run(trades.telegram_webhook(payload, secret="secret"))
    asyncio.run(trades.telegram_webhook(payload, secret="secret"))
    assert len(sent) == 1
