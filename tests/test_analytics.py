from unittest.mock import MagicMock, patch

from analytics import clear_scan_performance_metrics, sanitize_event


def test_unknown_event_is_ignored():
    assert sanitize_event({"name": "custom_event", "properties": {}}) is None


def test_only_allowlisted_properties_are_retained():
    result = sanitize_event({
        "name": "tab_open",
        "properties": {
            "tab": "inventory",
            "search_query": "sensitive search",
            "telegram_id": 123456,
            "card_name": "private card name",
        },
    })
    assert result == ("tab_open", {"tab": "inventory"})


def test_invalid_enum_and_malformed_values_do_not_raise():
    assert sanitize_event({"name": [], "properties": {}}) is None
    result = sanitize_event({
        "name": "scan_failed",
        "properties": {"reason": ["server"], "profile": {"total_ms": "fast"}},
    })
    assert result == ("scan_failed", {})


def test_scan_metrics_are_bounded_and_profile_is_allowlisted():
    result = sanitize_event({
        "name": "scan_completed",
        "properties": {
            "cards_detected": 100000,
            "matched_count": -3,
            "duration_ms": 900000,
            "profile": {
                "detection_ms": 23.45,
                "total_ms": 100.06,
                "image_path": "/private/path",
            },
        },
    })
    assert result == (
        "scan_completed",
        {
            "cards_detected": 10000,
            "matched_count": 0,
            "duration_ms": 600000.0,
            "profile": {"detection_ms": 23.4, "total_ms": 100.1},
        },
    )


def test_invalid_rating_is_dropped():
    assert sanitize_event({"name": "feedback_submitted", "properties": {"rating": 10}}) == (
        "feedback_submitted",
        {},
    )


def test_feedback_rating_and_category_are_recorded_without_free_text():
    result = sanitize_event({
        "name": "feedback_submitted",
        "properties": {"rating": 4, "category": "confusing", "comment": "private free text"},
    })
    assert result == ("feedback_submitted", {"category": "confusing", "rating": 4})



def test_clear_scan_performance_removes_only_timing_fields():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor_context = connection.cursor.return_value
    cursor = cursor_context.__enter__.return_value
    cursor.fetchall.return_value = [{"id": 1}, {"id": 2}]

    with patch("analytics.get_connection", return_value=connection):
        cleared = clear_scan_performance_metrics()

    assert cleared == 2
    query = cursor.execute.call_args.args[0]
    assert "properties - 'duration_ms' - 'profile'" in query
    assert "event_name IN ('scan_completed', 'scan_failed')" in query
    assert "RETURNING id" in query
    connection.commit.assert_called_once()
