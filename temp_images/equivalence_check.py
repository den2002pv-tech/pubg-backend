"""Local replica of the admin "Качество скриншотов" checks (equivalence baseline).

Runs scan_screenshot + catalog matching + IoU/quantity/identity checks on all
fixtures and dumps a JSON snapshot. Used to prove that optimizations change
nothing in the scanner outputs (hashes, boxes, quantities, match_score).

This is NOT the server test: no resource monitoring, no branch fixture fetch.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import main  # noqa: E402
from main import (  # noqa: E402
    _enrich_admin_scan,
    _has_counter_evidence,
    _identity_hash_distances,
    _match_card_detailed,
    clean_hashes,
    load_db,
)
from scanner import scan_screenshot  # noqa: E402

CASES_PATH = ROOT / "tests" / "fixtures" / "screenshots" / "cases.json"


def _empty_identity_detail(card: dict) -> dict:
    return {
        "expected_card_id": str(card.get("card_id") or ""),
        "matched_card_id": None, "matched": False, "decision": "unmatched_other",
        "match_score": None, "runner_up_id": None, "runner_up_score": None, "margin": None,
        "strong_count": 0, "acceptable_count": 0, "color_ratio": None, "color_penalty": None,
    }


def _check_case(case: dict, catalog: dict) -> dict:
    path = ROOT / case["image_path"]
    with Image.open(path) as source:
        source.load()
        image = source.convert("RGB")
    scan_started = time.perf_counter()
    result = scan_screenshot(image, save_previews=False)
    scan_ms = (time.perf_counter() - scan_started) * 1000.0
    detected = [c for c in result.get("cards", []) if isinstance(c, dict)]
    catalog_matches = _enrich_admin_scan(detected, catalog)

    owned = [c for c in case.get("cards", []) if c.get("state", "owned") == "owned"]
    errors: list[str] = []
    if len(owned) != len(detected):
        errors.append(f"Количество карт: ожидалось {len(owned)}, найдено {len(detected)}")

    unmatched = list(detected)
    matched = checked = 0
    identity_matches = 0
    identity_details: list[dict] = []
    for expected in owned:
        box = expected.get("box", [])
        ex, ey, ew, eh = box
        best, best_iou = None, 0.0
        for actual in unmatched:
            ax, ay, aw, ah = actual["box"]
            ix, iy = max(ex, ax), max(ey, ay)
            iw = max(0, min(ex + ew, ax + aw) - ix)
            ih = max(0, min(ey + eh, ay + ah) - iy)
            inter = iw * ih
            union = ew * eh + aw * ah - inter
            score = inter / union if union else 0.0
            if score > best_iou:
                best, best_iou = actual, score
        label = expected.get("card_name") or expected.get("card_id") or "Карта"
        if best is None or best_iou < 0.70:
            errors.append(f"{label}: IoU {best_iou:.2f}")
            if expected.get("identity_verified") is True:
                identity_details.append(_empty_identity_detail(expected))
            continue
        actual_index = next(i for i, c in enumerate(detected) if c is best)
        unmatched.remove(best)
        ax, ay, aw, ah = best["box"]
        ix, iy = max(ex, ax), max(ey, ay)
        inter = max(0, min(ex + ew, ax + aw) - ix) * max(0, min(ey + eh, ay + ah) - iy)
        coverage = inter / (ew * eh) if ew * eh else 0
        precision = inter / (aw * ah) if aw * ah else 0
        if coverage < 0.90:
            errors.append(f"{label}: покрытие {coverage:.0%}")
        if precision < 0.75:
            errors.append(f"{label}: точность кропа {precision:.0%}")
        checked += 1
        actual_quantity = int(best.get("quantity", 1))
        expected_quantity = int(expected.get("expected_quantity", 1))
        if actual_quantity == expected_quantity:
            matched += 1
        else:
            errors.append(f"{label}: ожидалось {expected_quantity}, найдено {actual_quantity}")

        if expected.get("identity_verified") is True:
            expected_id = str(expected.get("card_id") or "").strip()
            actual_identity = (
                catalog_matches[actual_index]
                if actual_index is not None and actual_index < len(catalog_matches)
                and isinstance(catalog_matches[actual_index], dict) else {}
            )
            actual_id = str(actual_identity.get("id") or "").strip() if actual_identity.get("matched") else ""
            scan_hashes = clean_hashes(best.get("hashes"))
            raw_hashes = clean_hashes(best.get("raw_hashes"))
            detail = _match_card_detailed(scan_hashes, catalog)
            if raw_hashes != scan_hashes:
                raw_detail = _match_card_detailed(raw_hashes, catalog)
                if raw_detail.get("id") is not None and (detail.get("score") is None or raw_detail["score"] < detail["score"]):
                    detail, scan_hashes = raw_detail, raw_hashes
            runner_id = detail.get("runner_up_id")
            is_unknown_ok = (not expected_id) and (not actual_id)
            is_known_ok = bool(actual_id) and actual_id == expected_id
            if is_unknown_ok or is_known_ok:
                decision = "matched_ok"
            elif actual_id:
                decision = "wrong_id"
            else:
                decision = detail.get("reason", "unmatched_other")
            if decision == "matched_ok":
                identity_matches += 1
            else:
                errors.append(f"{label}: ID ожидалось {expected_id or 'неизвестная'}, распознано {actual_id or 'нет'}")
            score_value = detail.get("score") if actual_id else detail.get("candidate_score")
            identity_details.append({
                "expected_card_id": expected_id,
                "matched_card_id": actual_id or None,
                "matched": bool(actual_id),
                "decision": decision,
                "match_score": round(float(score_value), 3) if score_value is not None else None,
                "runner_up_id": runner_id,
                "runner_up_score": round(float(detail["runner_up_score"]), 3) if detail.get("runner_up_score") is not None else None,
                "margin": round(float(detail["margin"]), 3) if detail.get("margin") is not None and math.isfinite(float(detail["margin"])) else None,
                "strong_count": int(detail.get("strong_count", 0)),
                "acceptable_count": int(detail.get("acceptable_count", 0)),
                "color_ratio": round(float(detail["color_ratio"]), 4) if detail.get("color_ratio") is not None else None,
                "color_penalty": round(float(detail["color_penalty"]), 4) if detail.get("color_penalty") is not None else None,
                "distances_to_expected": _identity_hash_distances(scan_hashes, catalog.get(expected_id)),
                "hash_reuse": bool(raw_hashes) and scan_hashes == raw_hashes,
                "hashes": scan_hashes,
                "raw_hashes": raw_hashes,
                "quantity": {"expected": expected_quantity, "detected": actual_quantity},
                "box": [ax, ay, aw, ah],
                "counter_debug": best.get("counter_debug", {}),
                "quantity_confidence": best.get("quantity_confidence"),
            })

    profile = result.get("profile", {})
    return {
        "id": case["id"],
        "passed": not errors,
        "errors": errors,
        "expected_cards": len(owned),
        "detected_cards": len(detected),
        "quantity_matches": matched,
        "quantity_checked": checked,
        "identity_matches": identity_matches,
        "identity_details": identity_details,
        "scan_ms": round(scan_ms, 1),
        "profile": {k: v for k, v in profile.items() if k not in ("hash_input_dimensions",)},
    }


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "temp_images" / "baseline_before.json"
    manifest = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    catalog = load_db()
    results = []
    for case in manifest["cases"]:
        case_result = _check_case(case, catalog)
        results.append(case_result)
        print(f"{case_result['id']}: passed={case_result['passed']} "
              f"detected={case_result['detected_cards']} qty={case_result['quantity_matches']}/"
              f"{case_result['quantity_checked']} identity={case_result['identity_matches']} "
              f"scan_ms={case_result['scan_ms']}")
    passed = sum(r["passed"] for r in results)
    identity = sum(r["identity_matches"] for r in results)
    identity_checked = sum(
        1 for r in results for d in r["identity_details"]
    )
    total_scan = sum(r["scan_ms"] for r in results)
    print(f"\nSUMMARY: cases {passed}/{len(results)}, identity {identity}/{identity_checked}, "
          f"scan_ms total={round(total_scan, 1)}")
    out_path.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
