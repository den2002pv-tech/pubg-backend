"""
analytics.py — Аналитика с поддержкой PostgreSQL и SQLite (вариант B: Python-постобработка).

В SQLite-режиме JSONB-операторы (->>, ?, -) заменены Python-постобработкой.
Все функции публичного интерфейса сохранены.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import psycopg

from database import get_connection
from database_manager import get_manager
from db_sqlite_schema import parse_properties, remove_json_fields

ALLOWED_EVENTS = {
    "app_open", "tab_open", "scan_started", "scan_completed", "scan_failed", "counter_corrected",
    "filter_changed", "sort_changed", "search_used", "collection_saved",
    "wishlist_updated", "trade_viewed", "trade_action", "feedback_submitted",
}
ALLOWED_TABS = {"scanner", "inventory", "trades"}
ALLOWED_FILTERS = {"all", "not_desired", "desired", "owned", "duplicates", "missing"}
ALLOWED_SORTS = {"name", "rarity", "quantity", "duplicates", "id"}
ALLOWED_FAILURES = {"timeout", "network", "server", "unknown"}
ALLOWED_TRADE_ACTIONS = {"open", "accept", "decline", "cancel", "confirm", "code_submit", "request"}
ALLOWED_FEEDBACK_CATEGORIES = {"bug", "slow", "confusing", "feature", "other"}
PROFILE_TIME_KEYS = {
    "rgb_convert_ms", "request_body_read_ms", "queue_wait_ms", "upload_read_ms",
    "upload_image_decode_ms",
    "upload_rgb_convert_ms", "detection_ms", "counter_ms", "counter_image_prepare_ms",
    "counter_threshold_ms", "counter_fallback_ms", "raw_hash_ms", "clean_hash_ms",
    "hash_preparation_ms", "hash_frame_extract_ms", "hash_compute_ms", "preview_ms",
    "catalog_load_ms", "matching_evaluation_ms", "matching_ms", "total_ms",
    "server_total_ms", "client_form_data_ms",
    "client_wake_ms", "client_upload_ms", "client_response_wait_ms",
    "client_response_parse_ms", "client_request_ms",
}
PROFILE_COUNT_KEYS = {
    "card_crop_count", "counter_attempts", "counter_threshold_passes",
    "counter_fallback_passes", "counter_contours", "hash_calculation_count",
    "hash_operation_count", "raw_hash_calculation_count",
    "clean_hash_calculation_count", "hash_reuse_count", "matching_calls",
    "catalog_candidates_before_filter", "matching_hash_comparisons",
    "matching_candidates_after_filter", "matched_count",
}
PROFILE_DIMENSION_KEYS = {
    "input_width", "input_height", "upload_source_width", "upload_source_height",
}
PROFILE_SIZE_KEYS = {"upload_size_bytes", "client_file_size_bytes"}
ALLOWED_IMAGE_FORMATS = {"BMP", "GIF", "JPEG", "PNG", "TIFF", "WEBP", "UNKNOWN", "OTHER"}


def init_analytics_db() -> None:
    """Создать таблицу аналитики (работает с активной БД через get_connection)."""
    mgr = get_manager()
    if mgr.is_postgres:
        _init_analytics_db_pg()
    # SQLite-схема уже создана через db_sqlite_schema.ensure_sqlite_schema


def _init_analytics_db_pg() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_analytics_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    event_name TEXT NOT NULL,
                    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_user_analytics_created
                    ON user_analytics_events (created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_analytics_name_created
                    ON user_analytics_events (event_name, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_analytics_user_created
                    ON user_analytics_events (user_id, created_at DESC);
            """)
        conn.commit()
        from db_sync import ensure_postgres_outbox_schema
        ensure_postgres_outbox_schema(conn)
        conn.commit()


def _sanitize_enum_properties(raw: dict[str, Any]) -> dict[str, Any]:
    enum_fields = {
        "tab": ALLOWED_TABS,
        "filter": ALLOWED_FILTERS,
        "sort": ALLOWED_SORTS,
        "reason": ALLOWED_FAILURES,
        "action": ALLOWED_TRADE_ACTIONS,
        "category": ALLOWED_FEEDBACK_CATEGORIES,
    }
    return {
        key: value
        for key, choices in enum_fields.items()
        if isinstance(value := raw.get(key), str) and value in choices
    }


def _sanitize_count_properties(raw: dict[str, Any]) -> dict[str, Any]:
    count_keys = (
        "cards_detected", "matched_count", "added_count",
        "removed_count", "card_count", "corrected_count",
    )
    return {
        key: max(0, min(int(value), 10000))
        for key in count_keys
        if isinstance(value := raw.get(key), (int, float)) and not isinstance(value, bool)
    }


def _sanitize_scan_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    scan_id = raw.get("scan_id")
    if (
        isinstance(scan_id, str)
        and 1 <= len(scan_id) <= 64
        and all(char.isalnum() or char in "-_" for char in scan_id)
    ):
        props["scan_id"] = scan_id

    occurred_at = raw.get("occurred_at")
    if isinstance(occurred_at, str) and len(occurred_at) <= 64:
        try:
            parsed_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
            if parsed_at.tzinfo is not None:
                props["occurred_at"] = (
                    parsed_at.astimezone(timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
        except ValueError:
            pass
    return props


def _sanitize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    profile_raw = raw.get("profile")
    if not isinstance(profile_raw, dict):
        return {}
    profile: dict[str, Any] = {}
    for key in PROFILE_TIME_KEYS:
        val = profile_raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            bounded = max(0.0, min(float(val), 600_000.0))
            profile[key] = round(bounded, 1)
    for key in PROFILE_COUNT_KEYS:
        val = profile_raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            profile[key] = max(0, min(int(val), 10_000))
    for key in PROFILE_DIMENSION_KEYS:
        val = profile_raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            profile[key] = max(0, min(int(val), 100_000))
    for key in PROFILE_SIZE_KEYS:
        val = profile_raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            profile[key] = max(0, min(int(val), 200_000_000))
    image_format = profile_raw.get("upload_format")
    if isinstance(image_format, str):
        profile["upload_format"] = image_format.upper() if image_format.upper() in ALLOWED_IMAGE_FORMATS else "OTHER"
    return profile


def _sanitize_duration(raw: dict[str, Any]) -> dict[str, Any]:
    duration_ms = raw.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        return {"duration_ms": max(0.0, min(float(duration_ms), 600_000.0))}
    return {}


def sanitize_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    name = event.get("name")
    if not isinstance(name, str) or name not in ALLOWED_EVENTS:
        return None
    raw = event.get("properties") or {}
    if not isinstance(raw, dict):
        return None
    props: dict[str, Any] = {}
    props.update(_sanitize_enum_properties(raw))
    props.update(_sanitize_count_properties(raw))
    props.update(_sanitize_scan_metadata(raw))
    props.update(_sanitize_duration(raw))
    rating = raw.get("rating")
    if name == "feedback_submitted" and isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5:
        props["rating"] = rating
    profile = _sanitize_profile(raw)
    if profile:
        props["profile"] = profile
    return name, props


def record_analytics_events(user_id: int, events: list[dict[str, Any]]) -> int:
    """Записать аналитические события. Работает с активной БД."""
    sanitized = [sanitize_event(e) for e in events if isinstance(e, dict)]
    valid = [(name, props) for item in sanitized if item is not None for name, props in [item]]
    if not valid:
        return 0
    return _record_analytics_pg(user_id, valid)


def _record_analytics_pg(user_id: int, valid_events: list[tuple[str, dict]]) -> int:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for event_name, properties in valid_events:
                    cur.execute(
                        """INSERT INTO user_analytics_events (user_id, event_name, properties)
                           VALUES (%s, %s, %s::jsonb) RETURNING id, created_at""",
                        (user_id, event_name, json.dumps(properties, ensure_ascii=False)),
                    )
            conn.commit()
        return len(valid_events)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        get_manager().notify_pg_write_failed(RuntimeError("PostgreSQL недоступна при записи аналитики"))
        raise


def get_analytics_summary(days: int = 30) -> dict[str, Any]:
    """Агрегированная сводка аналитики. Поддерживает PG и SQLite."""
    days = max(1, min(int(days), 90))
    mgr = get_manager()
    if mgr.is_postgres:
        return _get_analytics_summary_pg(days)
    else:
        return _get_analytics_summary_sqlite(days)


def _get_analytics_summary_pg(days: int) -> dict[str, Any]:
    events = []
    daily_active = []
    popular_tabs = []
    scans = {}
    active_users = 0
    feedback = {}
    feedback_categories = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_name, COUNT(*) AS events
                FROM user_analytics_events
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY event_name ORDER BY events DESC, event_name
            """, (days,))
            events = [dict(row) for row in cur.fetchall()]
            cur.execute("""
                SELECT DATE(created_at) AS day, COUNT(DISTINCT user_id) AS active_users
                FROM user_analytics_events
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY DATE(created_at) ORDER BY day
            """, (days,))
            daily_active = [{"day": row["day"].isoformat(), "active_users": int(row["active_users"])} for row in cur.fetchall()]
            cur.execute("""
                SELECT properties->>'tab' AS tab, COUNT(*) AS views,
                       COUNT(DISTINCT user_id) AS users
                FROM user_analytics_events
                WHERE event_name = 'tab_open'
                  AND created_at >= NOW() - (%s * INTERVAL '1 day')
                  AND properties ? 'tab'
                GROUP BY properties->>'tab' ORDER BY views DESC
            """, (days,))
            popular_tabs = [dict(row) for row in cur.fetchall()]
            cur.execute("""
                SELECT COUNT(*) AS scans,
                       COUNT(*) FILTER (WHERE event_name = 'scan_failed') AS failed_scans,
                       ROUND(AVG((properties->>'duration_ms')::numeric) FILTER (WHERE event_name = 'scan_completed' AND properties ? 'duration_ms'), 1) AS avg_duration_ms,
                       ROUND(AVG((properties->>'matched_count')::numeric) FILTER (WHERE event_name = 'scan_completed' AND properties ? 'matched_count'), 1) AS avg_matched_cards
                FROM user_analytics_events
                WHERE event_name IN ('scan_completed', 'scan_failed')
                  AND created_at >= NOW() - (%s * INTERVAL '1 day')
            """, (days,))
            scans = dict(cur.fetchone())
            cur.execute("""
                SELECT COUNT(DISTINCT user_id) AS active_users
                FROM user_analytics_events
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
            """, (days,))
            active_users = int(cur.fetchone()["active_users"])
            cur.execute("""
                SELECT COUNT(*) AS ratings,
                       ROUND(AVG((properties->>'rating')::numeric), 1) AS average_rating
                FROM user_analytics_events
                WHERE event_name = 'feedback_submitted'
                  AND properties ? 'rating'
                  AND created_at >= NOW() - (%s * INTERVAL '1 day')
            """, (days,))
            feedback = dict(cur.fetchone())
            cur.execute("""
                SELECT properties->>'category' AS category, COUNT(*) AS ratings
                FROM user_analytics_events
                WHERE event_name = 'feedback_submitted'
                  AND properties ? 'category'
                  AND created_at >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY properties->>'category' ORDER BY ratings DESC
            """, (days,))
            feedback_categories = [dict(row) for row in cur.fetchall()]
    return {
        "days": days, "active_users": active_users, "events": events,
        "daily_active": daily_active, "popular_tabs": popular_tabs,
        "scans": scans, "feedback": feedback, "feedback_categories": feedback_categories,
    }


def _get_analytics_summary_sqlite(days: int) -> dict[str, Any]:
    """Python-постобработка JSONB-агрегаций для SQLite (вариант B)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        rows = con.execute(
            "SELECT event_name, properties, created_at, user_id FROM user_analytics_events WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()

    # Постобработка в Python
    event_counts: dict[str, int] = {}
    daily_users: dict[str, set] = {}
    tab_views: dict[str, int] = {}
    tab_users: dict[str, set] = {}
    scan_count = 0
    scan_failed = 0
    scan_durations: list[float] = []
    scan_matched: list[float] = []
    all_users: set = set()
    ratings: list[float] = []
    categories: dict[str, int] = {}

    for row in rows:
        event_name = row["event_name"]
        props = parse_properties(row["properties"])
        created_at_str = row["created_at"]
        user_id = row["user_id"]

        # event count
        event_counts[event_name] = event_counts.get(event_name, 0) + 1
        all_users.add(user_id)

        # daily active
        try:
            day_str = created_at_str[:10] if isinstance(created_at_str, str) else str(created_at_str)[:10]
        except Exception:
            day_str = "unknown"
        daily_users.setdefault(day_str, set()).add(user_id)

        # tabs
        if event_name == "tab_open" and "tab" in props:
            tab = props["tab"]
            tab_views[tab] = tab_views.get(tab, 0) + 1
            tab_users.setdefault(tab, set()).add(user_id)

        # scans
        if event_name in ("scan_completed", "scan_failed"):
            scan_count += 1
            if event_name == "scan_failed":
                scan_failed += 1
            if event_name == "scan_completed":
                dur = props.get("duration_ms")
                if isinstance(dur, (int, float)):
                    scan_durations.append(float(dur))
                matched = props.get("matched_count")
                if isinstance(matched, (int, float)):
                    scan_matched.append(float(matched))

        # feedback
        if event_name == "feedback_submitted":
            rating = props.get("rating")
            if isinstance(rating, (int, float)):
                ratings.append(float(rating))
            cat = props.get("category")
            if isinstance(cat, str):
                categories[cat] = categories.get(cat, 0) + 1

    events_list = sorted(
        [{"event_name": k, "events": v} for k, v in event_counts.items()],
        key=lambda x: (-x["events"], x["event_name"]),
    )
    daily_active = sorted(
        [{"day": day, "active_users": len(users)} for day, users in daily_users.items()],
        key=lambda x: x["day"],
    )
    popular_tabs = sorted(
        [{"tab": tab, "views": tab_views[tab], "users": len(tab_users[tab])} for tab in tab_views],
        key=lambda x: -x["views"],
    )
    avg_dur = round(sum(scan_durations) / len(scan_durations), 1) if scan_durations else None
    avg_matched = round(sum(scan_matched) / len(scan_matched), 1) if scan_matched else None
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
    feedback_cats = sorted(
        [{"category": k, "ratings": v} for k, v in categories.items()],
        key=lambda x: -x["ratings"],
    )

    return {
        "days": days,
        "active_users": len(all_users),
        "events": events_list,
        "daily_active": daily_active,
        "popular_tabs": popular_tabs,
        "scans": {
            "scans": scan_count,
            "failed_scans": scan_failed,
            "avg_duration_ms": avg_dur,
            "avg_matched_cards": avg_matched,
        },
        "feedback": {
            "ratings": len(ratings),
            "average_rating": avg_rating,
        },
        "feedback_categories": feedback_cats,
    }


def get_analytics_events(days: int = 30) -> dict[str, Any]:
    """Экспорт аналитических событий."""
    days = max(1, min(int(days), 90))
    mgr = get_manager()
    if mgr.is_postgres:
        return _get_analytics_events_pg(days)
    else:
        return _get_analytics_events_sqlite(days)


def _get_analytics_events_pg(days: int) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_name, properties, created_at
                FROM user_analytics_events
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
                ORDER BY created_at DESC, id DESC
            """, (days,))
            rows = cur.fetchall()
    return {
        "days": days,
        "events": [
            {
                "event_name": row["event_name"],
                "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
                "properties": row["properties"],
            }
            for row in rows
        ],
    }


def _get_analytics_events_sqlite(days: int) -> dict[str, Any]:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mgr = get_manager()
    with mgr.get_sqlite_connection() as con:
        rows = con.execute(
            """SELECT event_name, properties, created_at
               FROM user_analytics_events
               WHERE created_at >= ?
               ORDER BY created_at DESC, id DESC""",
            (cutoff,),
        ).fetchall()
    return {
        "days": days,
        "events": [
            {
                "event_name": row["event_name"],
                "created_at": row["created_at"] if isinstance(row["created_at"], str) else str(row["created_at"]),
                "properties": parse_properties(row["properties"]),
            }
            for row in rows
        ],
    }


def clear_scan_performance_metrics() -> int:
    """Удалить timing-поля из scan-событий. Работает с активной БД."""
    return _clear_scan_metrics_pg()


def _clear_scan_metrics_pg() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_analytics_events
                SET properties = properties - 'duration_ms' - 'profile'
                WHERE event_name IN ('scan_completed', 'scan_failed')
                  AND (properties ? 'duration_ms' OR properties ? 'profile')
                RETURNING id
            """)
            cleared = len(cur.fetchall())
        conn.commit()
    return cleared


