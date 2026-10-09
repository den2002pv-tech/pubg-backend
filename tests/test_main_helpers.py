import json
import time

import pytest

import main


def test_clean_hashes_keeps_valid_values_and_normalizes_strings():
    assert main.clean_hashes({
        "full_dhash": " ab12 ",
        "visual_phash": 123,
        "empty": " ",
        "missing": None,
        "undefined": "undefined",
        "null": "NULL",
    }) == {
        "full_dhash": "ab12",
        "visual_phash": "123",
    }


@pytest.mark.parametrize("value", ["", None, "undefined", " null ", "NONE"])
def test_valid_hash_rejects_empty_or_placeholder_values(value):
    assert main.valid_hash(value) is False


@pytest.mark.parametrize("value", ["0", "abcd", " 1234 "])
def test_valid_hash_accepts_real_hash_values(value):
    assert main.valid_hash(value) is True


def test_hamming_distance_counts_different_bits():
    assert main._hamming("0", "f") == 4
    assert main._hamming("00", "03") == 2


def test_hamming_returns_none_for_invalid_hashes():
    assert main._hamming("not-hex", "01") is None


def test_session_token_is_valid_with_configured_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)

    token = main._make_session()

    assert main._valid_session(token) is True


def test_session_token_rejects_tampering(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    token = main._make_session()
    parts = token.split(".")
    parts[-1] = "0" * 64

    assert main._valid_session(".".join(parts)) is False


def test_session_token_rejects_expired_timestamp(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    token = main._make_session()
    now = time.time()
    monkeypatch.setattr(main.time, "time", lambda: now + main.SESSION_MAX_AGE + 10)

    assert main._valid_session(token) is False


def test_session_token_rejects_future_timestamp(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    token = main._make_session()
    now = time.time()
    monkeypatch.setattr(main.time, "time", lambda: now - 120)

    assert main._valid_session(token) is False


def test_load_db_returns_empty_dict_when_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "missing.json")

    assert main.load_db() == {}


def test_load_db_returns_empty_dict_for_invalid_json(monkeypatch, tmp_path):
    db_file = tmp_path / "broken.json"
    db_file.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr(main, "DB_FILE", db_file)

    assert main.load_db() == {}


def test_save_db_and_load_db_round_trip(monkeypatch, tmp_path):
    db_file = tmp_path / "cards.json"
    monkeypatch.setattr(main, "DB_FILE", db_file)
    expected = {
        "card_1": {"name": "Карта 1", "hashes": {"full_dhash": "abcd"}},
        "card_2": {"name": "Карта 2", "rarity": 3},
    }

    main.save_db(expected)

    assert json.loads(db_file.read_text(encoding="utf-8")) == expected
    assert main.load_db() == expected


def test_match_card_returns_exact_hash_match():
    hashes = {
        "full_phash": "aaaaaaaa",
        "full_dhash": "bbbbbbbb",
        "visual_phash": "cccccccc",
        "visual_dhash": "dddddddd",
        "frame_colorhash": "eeeeeeee",
    }
    db = {
        "card_1": {
            "name": "Карта 1",
            "hashes": dict(hashes),
        }
    }

    card_id, distance = main._match_card(hashes, db)

    assert card_id == "card_1"
    assert distance == 0


def test_match_card_does_not_match_when_hashes_are_missing():
    hashes = {
        "full_phash": "aaaaaaaa",
        "full_dhash": "bbbbbbbb",
    }
    db = {
        "card_1": {
            "name": "Карта 1",
            "hashes": {
                "full_phash": "aaaaaaaa",
                "full_dhash": "bbbbbbbb",
            },
        }
    }

    card_id, _distance = main._match_card(hashes, db)

    assert card_id is None



def test_normalize_collection_updates_is_idempotent_and_deduplicates_card_ids():
    updates = [
        {"id": " card_1 ", "quantity": 2},
        {"id": "card_2", "quantity": 1},
        {"id": "card_1", "quantity": 4},
        {"id": "card_1", "quantity": 3},
        {"name": "missing ID", "quantity": 8},
        {"id": "   ", "quantity": 9},
    ]

    first = main._normalize_collection_updates(updates)
    second = main._normalize_collection_updates(updates)

    assert first == [("card_1", 4), ("card_2", 1)]
    assert second == first


def test_normalize_collection_updates_clamps_quantity_and_rejects_invalid_values():
    assert main._normalize_collection_updates([
        {"id": "card_1", "quantity": -3},
        {"id": "card_2", "quantity": 1500},
    ]) == [("card_1", 0), ("card_2", 999)]

    with pytest.raises(main.HTTPException) as exc:
        main._normalize_collection_updates([{"id": "card_1", "quantity": "not-a-number"}])

    assert exc.value.status_code == 400
