"""Fast, isolated tests for database helper behavior.

These tests intentionally do not connect to PostgreSQL or use DATABASE_URL.
Database integration tests can be added separately with a dedicated TEST_DATABASE_URL.
"""

import database


def test_set_desired_cards_deduplicates_and_trims_ids(monkeypatch):
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            captured.setdefault("calls", []).append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(database, "get_desired_cards", lambda _telegram_id: [{"id": "card_1"}])

    result = database.set_desired_cards(12345, [" card_1 ", "card_1", "", "card_2"])

    assert result == [{"id": "card_1"}]
    assert captured["committed"] is True
    insert_calls = [
        params for query, params in captured["calls"]
        if "INSERT INTO desired_cards" in query
    ]
    assert insert_calls == [(["card_1", "card_2"], 12345)]


def test_set_desired_cards_with_empty_list_only_clears_existing_list(monkeypatch):
    captured = {"calls": []}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            captured["calls"].append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(database, "get_desired_cards", lambda _telegram_id: [])

    assert database.set_desired_cards(12345, []) == []
    assert captured["committed"] is True
    assert len(captured["calls"]) == 1
    assert "DELETE FROM desired_cards" in captured["calls"][0][0]


def test_set_user_card_quantity_clamps_negative_quantity_and_removes_wishlist(monkeypatch):
    captured = {"calls": []}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            captured["calls"].append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(database, "get_connection", lambda: FakeConnection())

    database.set_user_card_quantity(12345, "card_1", -7)

    assert captured["calls"][0][1] == ("card_1", 0, 12345)
    assert len(captured["calls"]) == 1
    assert captured["committed"] is True


def test_set_user_card_quantity_positive_quantity_removes_card_from_wishlist(monkeypatch):
    captured = {"calls": []}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            captured["calls"].append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(database, "get_connection", lambda: FakeConnection())

    database.set_user_card_quantity(12345, "card_1", 2)

    assert len(captured["calls"]) == 2
    assert "DELETE FROM desired_cards" in captured["calls"][1][0]
    assert captured["calls"][1][1] == (12345, "card_1")
