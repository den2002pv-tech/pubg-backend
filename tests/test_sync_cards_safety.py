import sqlite3

import pytest

import database
from database_manager import DatabaseManager
from db_sqlite_schema import ensure_sqlite_schema


@pytest.fixture
def catalogue_db(monkeypatch, tmp_path):
    path = tmp_path / "catalogue.sqlite3"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv(database.CARD_CATALOG_ALLOW_DELETE_ENV, raising=False)
    monkeypatch.delenv(database.CARD_CATALOG_MAX_SHRINK_PERCENT_ENV, raising=False)
    DatabaseManager._instance = None
    manager = DatabaseManager.get_instance()
    manager.initialize()
    ensure_sqlite_schema(str(path))
    with sqlite3.connect(path) as con:
        con.executemany(
            "INSERT INTO cards(id, name, rarity, icon) VALUES(?,?,1,'')",
            [(f"card_{i}", f"Card {i}") for i in range(10)],
        )
    try:
        yield path
    finally:
        manager.stop_backup()
        DatabaseManager._instance = None


def _card_ids(path):
    with sqlite3.connect(path) as con:
        return {row[0] for row in con.execute("SELECT id FROM cards")}


def test_catalogue_sync_does_not_delete_by_default(catalogue_db):
    database.sync_cards({"card_0": {"name": "Updated"}})
    assert len(_card_ids(catalogue_db)) == 10


def test_catalogue_sync_allows_small_shrink_when_explicitly_enabled(catalogue_db, monkeypatch):
    monkeypatch.setenv(database.CARD_CATALOG_ALLOW_DELETE_ENV, "true")
    database.sync_cards({f"card_{i}": {"name": f"Card {i}"} for i in range(9)})
    assert len(_card_ids(catalogue_db)) == 9


def test_catalogue_sync_refuses_shrink_over_limit(catalogue_db, monkeypatch):
    monkeypatch.setenv(database.CARD_CATALOG_ALLOW_DELETE_ENV, "1")
    monkeypatch.setenv(database.CARD_CATALOG_MAX_SHRINK_PERCENT_ENV, "10")
    with pytest.raises(RuntimeError, match="Refusing to delete existing cards"):
        database.sync_cards({"card_0": {"name": "Card 0"}})
    assert len(_card_ids(catalogue_db)) == 10


def test_catalogue_shrink_percent_must_be_valid(catalogue_db, monkeypatch):
    monkeypatch.setenv(database.CARD_CATALOG_ALLOW_DELETE_ENV, "1")
    monkeypatch.setenv(database.CARD_CATALOG_MAX_SHRINK_PERCENT_ENV, "101")
    with pytest.raises(ValueError, match="must be 0..100"):
        database.sync_cards({f"card_{i}": {"name": f"Card {i}"} for i in range(9)})
