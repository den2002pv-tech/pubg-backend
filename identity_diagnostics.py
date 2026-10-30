"""Identity and hash-match diagnostics for screenshot quality reports."""
from __future__ import annotations

from typing import Any


def _hamming_local(a: str, b: str) -> int | None:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return None


def clean_hashes_local(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, val in value.items():
        if val is None:
            continue
        s = str(val).strip()
        if not s or s.lower() in {"undefined", "null", "none"}:
            continue
        result[str(key)] = s
    return result


def _resolve_hamming():
    try:
        import main as main_mod
        return main_mod._hamming
    except Exception:
        return _hamming_local


def _resolve_clean_hashes():
    try:
        import main as main_mod
        return main_mod.clean_hashes
    except Exception:
        return clean_hashes_local


def hash_distance_map(
    scan_hashes: dict[str, Any] | None,
    stored_hashes: dict[str, Any] | None,
) -> dict[str, Any]:
    """Perceptual + frame color distances between two hash sets."""
    _hamming = _resolve_hamming()
    clean_hashes = _resolve_clean_hashes()
    scan_hashes = scan_hashes or {}
    stored_hashes = clean_hashes(stored_hashes) if stored_hashes else {}
    out: dict[str, Any] = {
        "full_phash": None,
        "full_dhash": None,
        "visual_phash": None,
        "visual_dhash": None,
        "frame_colorhash_bits": None,
        "frame_colorhash_ratio": None,
    }
    for key in ("full_phash", "full_dhash", "visual_phash", "visual_dhash"):
        a, b = scan_hashes.get(key), stored_hashes.get(key)
        if a and b:
            out[key] = _hamming(str(a), str(b))
    scan_color = scan_hashes.get("frame_colorhash")
    stored_color = stored_hashes.get("frame_colorhash")
    if scan_color and stored_color:
        bits = _hamming(str(scan_color), str(stored_color))
        if bits is not None:
            out["frame_colorhash_bits"] = bits
            out["frame_colorhash_ratio"] = round(
                bits / max(1, len(str(scan_color)) * 4), 4
            )
    return out


def identity_decision(
    expected_id: str,
    matched: bool,
    matched_id: str,
    top_matches: list[dict[str, Any]] | None,
) -> str:
    """Classify identity outcome for quality-report diagnostics."""
    expected_id = (expected_id or "").strip()
    matched_id = (matched_id or "").strip()
    if matched and matched_id:
        if expected_id and matched_id == expected_id:
            return "matched_ok"
        return "wrong_id"

    top_matches = top_matches or []
    if not top_matches:
        return "unmatched_no_candidates"

    best = top_matches[0]
    reasons = list(best.get("reasons") or [])
    reason_text = " ".join(str(r) for r in reasons)

    if "ambiguous_runner_up" in reason_text or "insufficient_match_confidence" in reason_text:
        return "unmatched_low_margin"
    if best.get("color_status") == "high" or "color" in reason_text:
        return "unmatched_color_reject"
    if "less_than_2_strong" in reason_text:
        return "unmatched_low_strong"
    if any("outlier" in str(r) for r in reasons):
        return "unmatched_outlier_hash"
    if (
        "less_than_3_hashes" in reason_text
        or "less_than_3_acceptable" in reason_text
        or (best.get("accepted") is False and not reasons)
    ):
        return "unmatched_no_candidates"
    return "unmatched_other"


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * p
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def build_identity_detail(
    expected: dict[str, Any],
    detected_card: dict[str, Any] | None,
    catalog_match: dict[str, Any] | None,
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """One identity_verified card row for the downloadable quality report."""
    clean_hashes = _resolve_clean_hashes()
    expected_id = str(expected.get("card_id") or "").strip()
    expected_name = str(
        expected.get("card_name")
        or (catalog.get(expected_id) or {}).get("name")
        or expected_id
        or ""
    )
    catalog_match = catalog_match if isinstance(catalog_match, dict) else {}
    detected_card = detected_card if isinstance(detected_card, dict) else {}
    matched = bool(catalog_match.get("matched"))
    matched_id = str(catalog_match.get("id") or "").strip() if matched else ""
    top_matches: list[dict[str, Any]] = []
    debug = catalog_match.get("debug") if isinstance(catalog_match.get("debug"), dict) else {}
    if isinstance(debug.get("top_matches"), list):
        top_matches = debug["top_matches"]

    decision = identity_decision(expected_id, matched, matched_id, top_matches)
    best_diag = top_matches[0] if top_matches else {}
    runner_diag = top_matches[1] if len(top_matches) > 1 else None

    scan_hashes = detected_card.get("hashes") or catalog_match.get("hashes") or {}
    if not isinstance(scan_hashes, dict):
        scan_hashes = {}
    raw_hashes = detected_card.get("raw_hashes") if isinstance(detected_card.get("raw_hashes"), dict) else {}
    hash_reuse = bool(raw_hashes) and raw_hashes == scan_hashes

    expected_stored = clean_hashes((catalog.get(expected_id) or {}).get("hashes")) if expected_id in catalog else {}
    matched_stored = clean_hashes((catalog.get(matched_id) or {}).get("hashes")) if matched_id in catalog else {}
    runner_id = None
    runner_score = None
    if isinstance(best_diag, dict) and best_diag.get("runner_up_id"):
        runner_id = best_diag.get("runner_up_id")
        runner_score = best_diag.get("runner_up_score")
    elif runner_diag is not None:
        runner_id = runner_diag.get("id")
        runner_score = runner_diag.get("score")
    runner_stored = clean_hashes((catalog.get(str(runner_id)) or {}).get("hashes")) if runner_id else {}

    match_score = catalog_match.get("distance")
    if match_score is None and isinstance(best_diag, dict):
        match_score = best_diag.get("score")
    margin = None
    if isinstance(best_diag, dict) and best_diag.get("score_margin") is not None:
        margin = best_diag.get("score_margin")
    elif (
        match_score is not None
        and runner_score is not None
        and isinstance(match_score, (int, float))
        and isinstance(runner_score, (int, float))
    ):
        margin = round(float(runner_score) - float(match_score), 2)

    box = detected_card.get("box") if isinstance(detected_card.get("box"), list) else None
    width = detected_card.get("width")
    height = detected_card.get("height")
    if box and len(box) == 4:
        width = width if width is not None else box[2]
        height = height if height is not None else box[3]

    counter_debug = detected_card.get("counter_debug") if isinstance(detected_card.get("counter_debug"), dict) else {}
    has_counter_evidence = bool(
        counter_debug
        or int(detected_card.get("quantity") or 1) > 1
        or int(expected.get("expected_quantity") or 1) > 1
    )

    return {
        "expected_card_id": expected_id or None,
        "expected_card_name": expected_name or None,
        "matched_card_id": matched_id or None,
        "matched": matched and bool(matched_id),
        "decision": decision,
        "match_score": match_score if isinstance(match_score, (int, float)) else None,
        "runner_up_id": str(runner_id) if runner_id else None,
        "runner_up_score": runner_score if isinstance(runner_score, (int, float)) else None,
        "margin": margin if isinstance(margin, (int, float)) else None,
        "strong_count": int(best_diag.get("strong") or 0) if best_diag else 0,
        "acceptable_count": int(best_diag.get("acceptable") or 0) if best_diag else 0,
        "color_ratio": best_diag.get("color_ratio") if best_diag else None,
        "color_penalty": (
            round(float(best_diag["color_ratio"]) * 8.0, 3)
            if best_diag and isinstance(best_diag.get("color_ratio"), (int, float))
            else None
        ),
        "distances_to_expected": hash_distance_map(scan_hashes, expected_stored),
        "distances_to_matched": (
            hash_distance_map(scan_hashes, matched_stored) if matched_id else None
        ),
        "distances_to_runner_up": (
            hash_distance_map(scan_hashes, runner_stored) if runner_id else None
        ),
        "crop": {"box": box, "width": width, "height": height},
        "quantity": {
            "expected": int(expected.get("expected_quantity") or 1),
            "detected": int(detected_card.get("quantity") or 1) if detected_card else None,
        },
        "has_counter_evidence": has_counter_evidence,
        "hash_reuse": hash_reuse,
    }


def aggregate_identity_stats(details: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary block for identity/hash diagnostics across a report run."""
    decision_keys = (
        "matched_ok",
        "wrong_id",
        "unmatched_no_candidates",
        "unmatched_low_strong",
        "unmatched_low_margin",
        "unmatched_color_reject",
        "unmatched_outlier_hash",
        "unmatched_other",
    )
    by_decision = {key: 0 for key in decision_keys}
    scores: list[float] = []
    margins: list[float] = []
    distance_buckets: dict[str, list[float]] = {
        "full_phash": [],
        "full_dhash": [],
        "visual_phash": [],
        "visual_dhash": [],
        "frame_colorhash_ratio": [],
    }
    confusions: dict[tuple[str, str, str], dict[str, Any]] = {}

    for row in details:
        decision = str(row.get("decision") or "unmatched_other")
        if decision not in by_decision:
            decision = "unmatched_other"
        by_decision[decision] += 1

        if decision == "matched_ok":
            if isinstance(row.get("match_score"), (int, float)):
                scores.append(float(row["match_score"]))
            if isinstance(row.get("margin"), (int, float)):
                margins.append(float(row["margin"]))
            dist = row.get("distances_to_expected") or {}
            for key in distance_buckets:
                value = dist.get(key)
                if isinstance(value, (int, float)):
                    distance_buckets[key].append(float(value))
            runner = row.get("runner_up_id")
            expected = row.get("expected_card_id")
            if runner and expected and isinstance(row.get("margin"), (int, float)) and float(row["margin"]) <= 8:
                key = (str(expected), str(runner), "runner_up")
                entry = confusions.setdefault(
                    key,
                    {
                        "expected_id": str(expected),
                        "other_id": str(runner),
                        "relation": "runner_up",
                        "count": 0,
                        "margins": [],
                        "scores": [],
                    },
                )
                entry["count"] += 1
                entry["margins"].append(float(row["margin"]))
                if isinstance(row.get("match_score"), (int, float)):
                    entry["scores"].append(float(row["match_score"]))

        if decision == "wrong_id":
            expected = row.get("expected_card_id")
            other = row.get("matched_card_id")
            if expected and other:
                key = (str(expected), str(other), "wrong_id")
                entry = confusions.setdefault(
                    key,
                    {
                        "expected_id": str(expected),
                        "other_id": str(other),
                        "relation": "wrong_id",
                        "count": 0,
                        "margins": [],
                        "scores": [],
                    },
                )
                entry["count"] += 1
                if isinstance(row.get("margin"), (int, float)):
                    entry["margins"].append(float(row["margin"]))
                if isinstance(row.get("match_score"), (int, float)):
                    entry["scores"].append(float(row["match_score"]))

    scores_sorted = sorted(scores)
    margins_sorted = sorted(margins)

    top_confusions = []
    for entry in sorted(
        confusions.values(),
        key=lambda item: (-item["count"], item["expected_id"], item["other_id"]),
    ):
        top_confusions.append({
            "expected_id": entry["expected_id"],
            "other_id": entry["other_id"],
            "relation": entry["relation"],
            "count": entry["count"],
            "avg_margin": (
                round(sum(entry["margins"]) / len(entry["margins"]), 3)
                if entry["margins"] else None
            ),
            "avg_score": (
                round(sum(entry["scores"]) / len(entry["scores"]), 3)
                if entry["scores"] else None
            ),
        })
        if len(top_confusions) >= 10:
            break

    checked = len(details)
    correct = by_decision["matched_ok"]
    wrong = by_decision["wrong_id"]
    unmatched = checked - correct - wrong

    return {
        "checked": checked,
        "correct": correct,
        "wrong": wrong,
        "unmatched": unmatched,
        "by_decision": by_decision,
        "score_correct": {
            "median": None if not scores_sorted else round(percentile(scores_sorted, 0.5) or 0.0, 3),
            "p90": None if not scores_sorted else round(percentile(scores_sorted, 0.9) or 0.0, 3),
            "max": None if not scores_sorted else round(scores_sorted[-1], 3),
            "count": len(scores_sorted),
        },
        "margin_correct": {
            "median": None if not margins_sorted else round(percentile(margins_sorted, 0.5) or 0.0, 3),
            "p90": None if not margins_sorted else round(percentile(margins_sorted, 0.9) or 0.0, 3),
            "min": None if not margins_sorted else round(margins_sorted[0], 3),
            "count": len(margins_sorted),
        },
        "hash_distance_correct_median": {
            key: (
                None if not values
                else round(percentile(sorted(values), 0.5) or 0.0, 4)
            )
            for key, values in distance_buckets.items()
        },
        "hash_distance_correct_p90": {
            key: (
                None if not values
                else round(percentile(sorted(values), 0.9) or 0.0, 4)
            )
            for key, values in distance_buckets.items()
        },
        "top_confusions": top_confusions,
    }
