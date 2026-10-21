from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from database import get_connection

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
PROFILE_KEYS = {"rgb_convert_ms", "upload_decode_ms", "detection_ms", "counter_ms", "raw_hash_ms", "clean_hash_ms", "preview_ms", "matching_ms", "total_ms", "server_total_ms"}


def init_analytics_db() -> None:
    """Create the bounded-schema analytics table without changing collection data."""
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


def sanitize_event(value: Any) -> tuple[str, dict[str, Any]] | None:
    """Accept only named events and small, non-identifying, enumerated properties."""
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or name not in ALLOWED_EVENTS:
        return None
    raw = value.get("properties")
    raw = raw if isinstance(raw, dict) else {}
    props: dict[str, Any] = {}

    enum_fields = {
        "tab": ALLOWED_TABS,
        "filter": ALLOWED_FILTERS,
        "sort": ALLOWED_SORTS,
        "reason": ALLOWED_FAILURES,
        "action": ALLOWED_TRADE_ACTIONS,
        "category": ALLOWED_FEEDBACK_CATEGORIES,
    }
    for key, choices in enum_fields.items():
        value = raw.get(key)
        if isinstance(value, str) and value in choices:
            props[key] = value

    for key in ("cards_detected", "matched_count", "added_count", "removed_count", "card_count", "corrected_count"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            props[key] = max(0, min(int(val), 10000))

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

    duration = raw.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        props["duration_ms"] = round(max(0.0, min(float(duration), 600000.0)), 1)

    rating = raw.get("rating")
    if isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5:
        props["rating"] = rating

    profile = raw.get("profile")
    if isinstance(profile, dict):
        safe_profile = {}
        for key in PROFILE_KEYS:
            val = profile.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                safe_profile[key] = round(max(0.0, min(float(val), 600000.0)), 1)
        if safe_profile:
            props["profile"] = safe_profile

    return name, props


def record_analytics_events(user_id: int, events: list[Any]) -> int:
    safe = [item for item in (sanitize_event(event) for event in events[:20]) if item]
    if not safe:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO user_analytics_events (user_id, event_name, properties) VALUES (%s, %s, %s::jsonb)",
                [(user_id, name, json.dumps(props, separators=(",", ":"))) for name, props in safe],
            )
        conn.commit()
    return len(safe)


def get_analytics_summary(days: int = 30) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_name, COUNT(*) AS events, COUNT(DISTINCT user_id) AS users
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
        "days": days,
        "active_users": active_users,
        "events": events,
        "daily_active": daily_active,
        "popular_tabs": popular_tabs,
        "scans": scans,
        "feedback": feedback,
        "feedback_categories": feedback_categories,
    }

def get_analytics_events(days: int = 30) -> dict[str, Any]:
    """Export analytics events for a bounded period without internal user identifiers."""
    days = max(1, min(int(days), 90))
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
                "created_at": row["created_at"].isoformat(),
                "properties": row["properties"],
            }
            for row in rows
        ],
    }


def clear_scan_performance_metrics() -> int:
    """Remove only scan timing fields, retaining events and all other analytics."""
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

