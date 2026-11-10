import json
import sqlite3

import analytics
from database_manager import DatabaseManager
from db_sqlite_schema import ensure_sqlite_schema


def test_clear_scan_metrics_removes_only_timing_fields(monkeypatch, tmp_path):
    path = tmp_path / "analytics.sqlite3"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", "")
    DatabaseManager._instance = None
    manager = DatabaseManager.get_instance()
    manager.initialize()
    ensure_sqlite_schema(str(path))
    analytics.init_analytics_db()
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO users (telegram_id) VALUES (123)")
        user_id = con.execute("SELECT id FROM users WHERE telegram_id=123").fetchone()[0]
        con.execute(
            "INSERT INTO user_analytics_events(user_id, event_name, properties) VALUES (?, ?, ?)",
            (user_id, "scan_completed", '{"duration_ms":12,"profile":{"total_ms":10},"kept":true}'),
        )
        con.execute(
            "INSERT INTO user_analytics_events(user_id, event_name, properties) VALUES (?, ?, ?)",
            (user_id, "app_open", '{"duration_ms":9}'),
        )

    try:
        assert analytics.clear_scan_performance_metrics() == 1
        with sqlite3.connect(path) as con:
            scan_props = con.execute(
                "SELECT properties FROM user_analytics_events WHERE event_name='scan_completed'"
            ).fetchone()[0]
            app_props = con.execute(
                "SELECT properties FROM user_analytics_events WHERE event_name='app_open'"
            ).fetchone()[0]
        assert json.loads(scan_props) == {"kept": True}
        assert json.loads(app_props) == {"duration_ms": 9}
    finally:
        manager.stop_backup()
        DatabaseManager._instance = None


def test_clear_scan_metrics_is_noop_without_timing_fields(monkeypatch, tmp_path):
    path = tmp_path / "analytics.sqlite3"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", "")
    DatabaseManager._instance = None
    manager = DatabaseManager.get_instance()
    manager.initialize()
    ensure_sqlite_schema(str(path))
    analytics.init_analytics_db()
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO users (telegram_id) VALUES (123)")
        user_id = con.execute("SELECT id FROM users WHERE telegram_id=123").fetchone()[0]
        con.execute(
            "INSERT INTO user_analytics_events(user_id, event_name, properties) VALUES (?, ?, ?)",
            (user_id, "scan_failed", '{"reason":"network"}'),
        )
    try:
        assert analytics.clear_scan_performance_metrics() == 0
    finally:
        manager.stop_backup()
        DatabaseManager._instance = None
