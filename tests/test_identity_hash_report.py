import main


def _catalog():
    hashes = {"full_phash": "0000", "full_dhash": "0000", "visual_phash": "0000", "visual_dhash": "0000", "frame_colorhash": "0000"}
    return {"card_1": {"name": "One", "hashes": hashes}}


def test_detailed_match_correct_identity():
    result = main._match_card_detailed(_catalog()["card_1"]["hashes"], _catalog())
    assert result["id"] == "card_1"
    assert result["reason"] == "matched"
    assert result["strong_count"] == 4
    assert result["distances"]["full_phash"] == 0


def test_detailed_match_reports_low_margin():
    catalog = _catalog()
    catalog["card_2"] = {"name": "Two", "hashes": dict(catalog["card_1"]["hashes"])}
    result = main._match_card_detailed(catalog["card_1"]["hashes"], catalog)
    assert result["id"] is None
    assert result["reason"] == "unmatched_low_margin"
    assert result["runner_up_id"]


def test_identity_stats_preserve_null_for_empty_correct_set():
    stats = main._build_identity_stats([{"identity_details": [{
        "expected_card_id": "card_1", "decision": "unmatched_low_margin",
        "match_score": 1.0, "runner_up_id": "card_2", "margin": 0.0,
    }]}])
    assert stats["checked"] == 1
    assert stats["by_decision"]["unmatched_low_margin"] == 1
    assert stats["score_correct"]["median"] is None
    assert stats["score_correct"]["count"] == 0
    assert stats["top_confusions"][0]["relation"] == "runner_up"
