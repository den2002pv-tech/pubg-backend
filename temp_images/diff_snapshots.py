"""Strict diff of two equivalence snapshots (baseline vs after-change)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def main() -> None:
    before = load(sys.argv[1])
    after = load(sys.argv[2])
    b = {r["id"]: r for r in before["results"]}
    a = {r["id"]: r for r in after["results"]}
    assert set(b) == set(a), "case id sets differ"

    problems = 0
    for case_id in b:
        rb, ra = b[case_id], a[case_id]
        if rb["passed"] != ra["passed"]:
            print(f"[{case_id}] passed: {rb['passed']} -> {ra['passed']}")
            problems += 1
        if rb["errors"] != ra["errors"]:
            print(f"[{case_id}] errors differ: {rb['errors']} -> {ra['errors']}")
            problems += 1
        if rb["detected_cards"] != ra["detected_cards"]:
            print(f"[{case_id}] detected {rb['detected_cards']} -> {ra['detected_cards']}")
            problems += 1
        if rb["quantity_matches"] != ra["quantity_matches"]:
            print(f"[{case_id}] qty {rb['quantity_matches']} -> {ra['quantity_matches']}")
            problems += 1
        db = {d["expected_card_id"]: d for d in rb["identity_details"]}
        da = {d["expected_card_id"]: d for d in ra["identity_details"]}
        if set(db) != set(da):
            print(f"[{case_id}] identity detail sets differ")
            problems += 1
            continue
        for key in db:
            xb, xa = db[key], da[key]
            for field in ("decision", "match_score", "runner_up_id", "runner_up_score",
                          "margin", "strong_count", "acceptable_count", "color_ratio",
                          "color_penalty", "hash_reuse", "box", "quantity",
                          "quantity_confidence", "matched_card_id"):
                if xb.get(field) != xa.get(field):
                    print(f"[{case_id}] {key} {field}: {xb.get(field)!r} -> {xa.get(field)!r}")
                    problems += 1
            if xb.get("hashes") != xa.get("hashes"):
                print(f"[{case_id}] {key} hashes differ: {xb['hashes']} -> {xa['hashes']}")
                problems += 1
            if xb.get("raw_hashes") != xa.get("raw_hashes"):
                print(f"[{case_id}] {key} raw_hashes differ")
                problems += 1
            if xb.get("distances_to_expected") != xa.get("distances_to_expected"):
                print(f"[{case_id}] {key} distances_to_expected differ")
                problems += 1
            cb, ca = xb.get("counter_debug", {}), xa.get("counter_debug", {})
            for field in ("roi", "candidates", "selected", "reason",
                          "x_candidates", "digit_candidates"):
                if cb.get(field) != ca.get(field):
                    print(f"[{case_id}] {key} counter_debug.{field}: "
                          f"{json.dumps(cb.get(field), ensure_ascii=False)[:200]} -> "
                          f"{json.dumps(ca.get(field), ensure_ascii=False)[:200]}")
                    problems += 1
            runs_b = cb.get("threshold_runs", [])
            runs_a = ca.get("threshold_runs", [])
            if len(runs_b) != len(runs_a):
                print(f"[{case_id}] {key} threshold_runs count {len(runs_b)} -> {len(runs_a)}")
                problems += 1
            else:
                for rb_run, ra_run in zip(runs_b, runs_a):
                    for field in ("threshold", "contours", "processed_contours",
                                  "bright_pixel_ratio", "x_candidates_added",
                                  "digit_candidates_added"):
                        if rb_run.get(field) != ra_run.get(field):
                            print(f"[{case_id}] {key} run {rb_run.get('threshold')} "
                                  f"{field}: {rb_run.get(field)!r} -> {ra_run.get(field)!r}")
                            problems += 1
        pb, pa = rb["profile"], ra["profile"]
        for field in ("card_crop_count", "darkened_filtered_count", "counter_attempts",
                      "counter_threshold_passes", "counter_fallback_passes", "counter_contours",
                      "raw_hash_calculation_count", "clean_hash_calculation_count",
                      "hash_reuse_count", "hash_calculation_count"):
            if pb.get(field) != pa.get(field):
                print(f"[{case_id}] profile.{field}: {pb.get(field)!r} -> {pa.get(field)!r}")
                problems += 1

    tb = sum(r["scan_ms"] for r in b.values())
    ta = sum(r["scan_ms"] for r in a.values())
    print(f"\nscan_ms total: {tb} -> {ta} ({(ta - tb) / tb * 100:+.1f}%)")
    if problems:
        print(f"MISMATCHES: {problems}")
        sys.exit(1)
    print("IDENTICAL: all hashes, boxes, quantities, match_score, counter decisions match.")


if __name__ == "__main__":
    main()
