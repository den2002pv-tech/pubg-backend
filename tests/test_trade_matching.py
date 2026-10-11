from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import database


def _row(telegram_id, username, give_id, give_name, get_id, get_name):
    return {
        "telegram_id": telegram_id,
        "username": username,
        "first_name": username.title(),
        "last_name": "",
        "receive_card_id": give_id,
        "receive_card_name": give_name,
        "receive_card_icon": "",
        "give_card_id": get_id,
        "give_card_name": get_name,
        "give_card_icon": "",
        "my_available": 2,
        "their_available": 1,
    }


def test_get_trade_matches_groups_reciprocal_pairs_and_sorts_by_count():
    rows = [
        _row(20, "player_b", "card_1", "Card 1", "card_8", "Card 8"),
        _row(20, "player_b", "card_2", "Card 2", "card_9", "Card 9"),
        _row(30, "player_c", "card_1", "Card 1", "card_8", "Card 8"),
    ]
    fake_cursor = MagicMock()
    fake_cursor.fetchall.return_value = rows
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    @contextmanager
    def fake_connection():
        yield fake_conn

    with patch.object(database, "get_connection", fake_connection):
        matches = database.get_trade_matches(10)

    assert [match["telegram_id"] for match in matches] == [20, 30]
    assert len(matches[0]["trades"]) == 2
    assert matches[0]["trades"][0]["you_give"]["id"] == "card_1"
    assert matches[0]["trades"][0]["you_get"]["id"] == "card_8"
    assert matches[0]["trades"][0]["you_give"]["available"] == 2
    assert matches[0]["trades"][0]["you_get"]["available"] == 1


def test_get_trade_matches_only_uses_surplus_and_explicit_wishlist():
    fake_cursor = MagicMock()
    fake_cursor.fetchall.return_value = []
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    @contextmanager
    def fake_connection():
        yield fake_conn

    with patch.object(database, "get_connection", fake_connection):
        assert database.get_trade_matches(10) == []

    query = fake_cursor.execute.call_args.args[0]
    assert "WHERE uc.quantity > 1" in query
    assert "WHERE COALESCE(uc.quantity, 0) = 0" in query
    assert "dc.user_id <> me.id" in query
