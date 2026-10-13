from analytics import sanitize_event


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
    assert sanitize_event({"name": "feedback_submitted", "properties": {"rating": 10}} == (
        "feedback_submitted",
        {},
    )
