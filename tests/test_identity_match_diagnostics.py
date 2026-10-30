"""Unit tests for identity/hash diagnostics in screenshot quality reports."""
from __future__ import annotations

import unittest

import identity_diagnostics as diag


class IdentityMatchDiagnosticsTests(unittest.TestCase):
    def test_hash_distance_map_reports_hamming_and_color_ratio(self):
        scan = {
            "full_phash": "0" * 16,
            "full_dhash": "0" * 16,
            "visual_phash": "0" * 16,
            "visual_dhash": "0" * 16,
            "frame_colorhash": "0" * 16,
        }
        stored = {
            "full_phash": "0" * 15 + "1",
            "full_dhash": "0" * 16,
            "visual_phash": "0" * 16,
            "visual_dhash": "0" * 16,
            "frame_colorhash": "0" * 15 + "1",
        }
        distances = diag.hash_distance_map(scan, stored)
        self.assertEqual(1, distances["full_phash"])
        self.assertEqual(0, distances["full_dhash"])
        self.assertEqual(1, distances["frame_colorhash_bits"])
        self.assertIsInstance(distances["frame_colorhash_ratio"], float)

    def test_identity_decision_matched_and_wrong(self):
        self.assertEqual("matched_ok", diag.identity_decision("card_1", True, "card_1", []))
        self.assertEqual("wrong_id", diag.identity_decision("card_1", True, "card_2", []))

    def test_identity_decision_unmatched_reasons(self):
        self.assertEqual("unmatched_no_candidates", diag.identity_decision("card_1", False, "", []))
        self.assertEqual(
            "unmatched_low_margin",
            diag.identity_decision("card_1", False, "", [{"reasons": ["ambiguous_runner_up"], "accepted": False}]),
        )
        self.assertEqual(
            "unmatched_low_strong",
            diag.identity_decision("card_1", False, "", [{"reasons": ["less_than_2_strong"], "accepted": False}]),
        )
        self.assertEqual(
            "unmatched_color_reject",
            diag.identity_decision("card_1", False, "", [{"reasons": [], "accepted": False, "color_status": "high"}]),
        )

    def test_build_identity_detail_matched_ok(self):
        catalog = {
            "card_1": {
                "name": "Alpha",
                "hashes": {
                    "full_phash": "0" * 16,
                    "full_dhash": "0" * 16,
                    "visual_phash": "0" * 16,
                    "visual_dhash": "0" * 16,
                    "frame_colorhash": "0" * 16,
                },
            },
            "card_2": {
                "name": "Beta",
                "hashes": {
                    "full_phash": "f" * 16,
                    "full_dhash": "f" * 16,
                    "visual_phash": "f" * 16,
                    "visual_dhash": "f" * 16,
                    "frame_colorhash": "f" * 16,
                },
            },
        }
        scan_hashes = dict(catalog["card_1"]["hashes"])
        detected = {
            "box": [10, 20, 100, 150],
            "width": 100,
            "height": 150,
            "quantity": 3,
            "hashes": scan_hashes,
            "raw_hashes": scan_hashes,
            "counter_debug": {"digits": [3]},
        }
        catalog_match = {
            "matched": True,
            "id": "card_1",
            "distance": 1.5,
            "hashes": scan_hashes,
            "debug": {
                "top_matches": [
                    {
                        "id": "card_1",
                        "score": 1.5,
                        "strong": 4,
                        "acceptable": 4,
                        "color_ratio": 0.01,
                        "reasons": [],
                        "accepted": True,
                        "runner_up_id": "card_2",
                        "runner_up_score": 12.0,
                        "score_margin": 10.5,
                    },
                    {"id": "card_2", "score": 12.0},
                ]
            },
        }
        detail = diag.build_identity_detail(
            {"card_id": "card_1", "card_name": "Alpha", "expected_quantity": 3, "identity_verified": True},
            detected,
            catalog_match,
            catalog,
        )
        self.assertEqual("matched_ok", detail["decision"])
        self.assertEqual("card_1", detail["matched_card_id"])
        self.assertEqual(10.5, detail["margin"])
        self.assertEqual(0, detail["distances_to_expected"]["full_phash"])
        self.assertTrue(detail["hash_reuse"])
        self.assertTrue(detail["has_counter_evidence"])

    def test_aggregate_identity_stats_counts_and_confusions(self):
        details = [
            {
                "decision": "matched_ok",
                "expected_card_id": "card_1",
                "matched_card_id": "card_1",
                "match_score": 2.0,
                "margin": 9.0,
                "runner_up_id": "card_9",
                "distances_to_expected": {
                    "full_phash": 2,
                    "full_dhash": 1,
                    "visual_phash": 2,
                    "visual_dhash": 1,
                    "frame_colorhash_ratio": 0.05,
                },
            },
            {
                "decision": "wrong_id",
                "expected_card_id": "card_2",
                "matched_card_id": "card_3",
                "match_score": 4.0,
                "margin": 1.0,
            },
            {
                "decision": "unmatched_low_margin",
                "expected_card_id": "card_4",
                "matched_card_id": None,
                "match_score": None,
                "margin": None,
            },
        ]
        stats = diag.aggregate_identity_stats(details)
        self.assertEqual(3, stats["checked"])
        self.assertEqual(1, stats["correct"])
        self.assertEqual(1, stats["wrong"])
        self.assertEqual(1, stats["unmatched"])
        self.assertEqual(1, stats["by_decision"]["matched_ok"])
        self.assertEqual(1, stats["by_decision"]["wrong_id"])
        self.assertEqual(1, stats["by_decision"]["unmatched_low_margin"])
        self.assertEqual(2.0, stats["score_correct"]["median"])
        self.assertEqual(1, len(stats["top_confusions"]))
        self.assertEqual("wrong_id", stats["top_confusions"][0]["relation"])


if __name__ == "__main__":
    unittest.main()
