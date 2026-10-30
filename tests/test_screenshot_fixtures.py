"""End-to-end regression tests for inventory screenshot detection and crops.

Real screenshots and their manually verified card rectangles live in
tests/fixtures/screenshots/. Each labelled case checks card count, crop
geometry/quality, and quantity on the exact crop produced by the scanner.
"""
import json
import unittest
from pathlib import Path

from PIL import Image

import main
from scanner import scan_screenshot

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "screenshots" / "cases.json"
MIN_IOU = 0.70
MIN_EXPECTED_COVERAGE = 0.90
MIN_CROP_PRECISION = 0.75
EDGE_TOLERANCE = 0.04


def _intersection_metrics(expected, actual):
    """Return IoU, expected-card coverage, and crop precision for xywh boxes."""
    ax, ay, aw, ah = actual
    bx, by, bw, bh = expected
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0, right - left) * max(0, bottom - top)
    expected_area = bw * bh
    actual_area = aw * ah
    union = expected_area + actual_area - intersection
    return (
        intersection / union if union else 0.0,
        intersection / expected_area if expected_area else 0.0,
        intersection / actual_area if actual_area else 0.0,
    )


def _iou(a, b):
    return _intersection_metrics(a, b)[0]


def _validate_box(box, image_size, label):
    if not isinstance(box, list) or len(box) != 4:
        raise AssertionError(f"{label}: expected box must be [x, y, width, height]")
    x, y, w, h = box
    if not all(isinstance(value, int) for value in box):
        raise AssertionError(f"{label}: box coordinates must be integers: {box}")
    if w <= 0 or h <= 0:
        raise AssertionError(f"{label}: box must have positive dimensions: {box}")
    image_w, image_h = image_size
    if x < 0 or y < 0 or x + w > image_w or y + h > image_h:
        raise AssertionError(
            f"{label}: box {box} extends outside screenshot {image_size}"
        )


def _assert_crop_quality(testcase, expected_box, actual_box, screenshot_size, label):
    """Ensure the detected crop contains the card and does not cut off its counter."""
    _validate_box(expected_box, screenshot_size, f"{label} reference")
    _validate_box(actual_box, screenshot_size, f"{label} detected crop")

    ex, ey, ew, eh = expected_box
    ax, ay, aw, ah = actual_box
    overlap, coverage, precision = _intersection_metrics(expected_box, actual_box)

    testcase.assertGreaterEqual(
        overlap, MIN_IOU,
        msg=f"{label}: crop IoU={overlap:.3f}; expected={expected_box}, actual={actual_box}",
    )
    testcase.assertGreaterEqual(
        coverage, MIN_EXPECTED_COVERAGE,
        msg=f"{label}: crop keeps only {coverage:.1%} of the labelled card; "
            f"expected={expected_box}, actual={actual_box}",
    )
    testcase.assertGreaterEqual(
        precision, MIN_CROP_PRECISION,
        msg=f"{label}: only {precision:.1%} of the crop overlaps the labelled card; "
            f"expected={expected_box}, actual={actual_box}",
    )

    # The quantity badge is in the card's top-right corner. A crop can have a
    # reasonable IoU while still clipping this corner, so check it explicitly.
    x_tolerance = max(2, int(ew * EDGE_TOLERANCE))
    y_tolerance = max(2, int(eh * EDGE_TOLERANCE))
    testcase.assertLessEqual(
        ax, ex + x_tolerance,
        msg=f"{label}: crop may cut off the card's left edge: {actual_box}",
    )
    testcase.assertLessEqual(
        ay, ey + y_tolerance,
        msg=f"{label}: crop may cut off the top/counter area: {actual_box}",
    )
    testcase.assertGreaterEqual(
        ax + aw, ex + ew - x_tolerance,
        msg=f"{label}: crop may cut off the card's right/counter area: {actual_box}",
    )
    testcase.assertGreaterEqual(
        ay + ah, ey + eh - y_tolerance,
        msg=f"{label}: crop may cut off the card's bottom edge: {actual_box}",
    )

    ratio = aw / float(ah)
    testcase.assertGreaterEqual(
        ratio, 0.45, msg=f"{label}: crop is too narrow for a card: ratio={ratio:.3f}",
    )
    testcase.assertLessEqual(
        ratio, 0.90, msg=f"{label}: crop is too wide for a card: ratio={ratio:.3f}",
    )
    testcase.assertGreaterEqual(aw, 20, msg=f"{label}: crop width is too small: {aw}")
    testcase.assertGreaterEqual(ah, 20, msg=f"{label}: crop height is too small: {ah}")


class ScreenshotFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = (
            json.loads(MANIFEST.read_text(encoding="utf-8")).get("cases", [])
            if MANIFEST.exists() else []
        )

    def test_screenshot_fixture_manifest_is_valid(self):
        self.assertTrue(MANIFEST.exists(), f"Missing fixture manifest: {MANIFEST}")
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(1, payload.get("version"), "Unsupported screenshot fixture version")
        self.assertIsInstance(payload.get("cases"), list)
        seen_ids = set()
        for case in payload["cases"]:
            self.assertIsInstance(case, dict)
            case_id = case.get("id")
            self.assertTrue(case_id, "Every screenshot fixture needs a stable id")
            self.assertNotIn(case_id, seen_ids, f"Duplicate screenshot fixture id: {case_id}")
            seen_ids.add(case_id)
            relative = Path(case.get("image_path", ""))
            self.assertFalse(relative.is_absolute(), f"{case_id}: image_path must be relative")
            self.assertNotIn("..", relative.parts, f"{case_id}: image_path cannot escape repository")
            self.assertTrue((ROOT / relative).is_file(), f"{case_id}: missing screenshot {relative}")
            self.assertIsInstance(case.get("cards"), list)
            self.assertGreater(len(case["cards"]), 0, f"{case_id}: add at least one labelled card")
            with Image.open(ROOT / relative) as source:
                actual_size = list(source.size)
            declared_size = case.get("source_screenshot_size", actual_size)
            self.assertEqual(actual_size, declared_size, f"{case_id}: screenshot dimensions changed")
            for card in case["cards"]:
                self.assertIn(card.get("state", "owned"), ("owned", "unowned"))
                _validate_box(card.get("box"), actual_size, case_id)
                quantity = card.get("expected_quantity", 1)
                self.assertIsInstance(quantity, int)
                self.assertGreaterEqual(quantity, 1)
                identity_verified = card.get("identity_verified", False)
                self.assertIsInstance(identity_verified, bool)
                if identity_verified:
                    self.assertEqual(
                        "owned", card.get("state", "owned"),
                        f"{case_id}: only owned cards can have a verified identity",
                    )

    def test_crop_quality_accepts_a_complete_card_crop(self):
        expected = [100, 100, 200, 300]
        actual = [102, 102, 196, 296]
        _assert_crop_quality(self, expected, actual, (500, 500), "synthetic-good-crop")

    def test_crop_quality_rejects_a_crop_that_clips_the_counter_corner(self):
        expected = [100, 100, 200, 300]
        # The top edge is clipped by more than the permitted tolerance.
        actual = [102, 116, 196, 284]
        with self.assertRaises(AssertionError):
            _assert_crop_quality(self, expected, actual, (500, 500), "synthetic-clipped-counter")

    def test_crop_quality_rejects_excessive_background(self):
        expected = [100, 100, 200, 300]
        actual = [50, 100, 300, 300]
        with self.assertRaises(AssertionError):
            _assert_crop_quality(self, expected, actual, (500, 500), "synthetic-background")

    def test_has_labeled_screenshot_cases(self):
        if not self.cases:
            self.skipTest(
                "No labelled full-screenshot fixtures yet; add manually checked screenshots "
                "before treating end-to-end coverage as complete"
            )
        self.assertGreater(len(self.cases), 0)

    def test_screenshot_card_count_crop_quality_and_quantities(self):
        if not self.cases:
            self.skipTest("No labelled full-screenshot fixtures have been saved yet")

        for case in self.cases:
            with self.subTest(case_id=case.get("id"), screenshot=case.get("image_path")):
                relative = Path(case["image_path"])
                screenshot_path = ROOT / relative
                with Image.open(screenshot_path) as source:
                    screenshot = source.convert("RGB")

                declared_size = case.get("source_screenshot_size")
                if declared_size is not None:
                    self.assertEqual(
                        declared_size, list(screenshot.size),
                        msg=f"{case['id']}: screenshot dimensions changed; do not resize fixtures",
                    )

                expected_cards = case["cards"]
                detected = scan_screenshot(screenshot, save_previews=False)["cards"]
                expected_owned = [c for c in expected_cards if c.get("state", "owned") == "owned"]
                expected_unowned = [c for c in expected_cards if c.get("state") == "unowned"]

                self.assertEqual(
                    len(expected_owned), len(detected),
                    msg=f"Expected {len(expected_owned)} owned cards, detected {len(detected)}; "
                        f"boxes={[card['box'] for card in detected]}",
                )

                unmatched = list(detected)
                for expected in expected_owned:
                    label = expected.get("card_name") or expected.get("card_id") or case["id"]
                    expected_box = expected["box"]
                    matches = sorted(
                        ((_iou(expected_box, item["box"]), item) for item in unmatched),
                        key=lambda pair: pair[0], reverse=True,
                    )
                    self.assertTrue(matches, f"{label}: no detected crop available")
                    _, actual = matches[0]
                    _assert_crop_quality(
                        self, expected_box, actual["box"], screenshot.size, label,
                    )
                    unmatched.remove(actual)

                    # Validate the actual crop dimensions/content path, not just
                    # the detector's reported rectangle.
                    x, y, w, h = actual["box"]
                    crop = screenshot.crop((x, y, x + w, y + h))
                    self.assertEqual((w, h), crop.size, f"{label}: crop dimensions disagree with box")
                    self.assertGreater(crop.width * crop.height, 0, f"{label}: empty crop")

                    expected_quantity = expected.get("expected_quantity", 1)
                    self.assertEqual(
                        expected_quantity, actual["quantity"],
                        msg=f"{label}: expected quantity {expected_quantity}, "
                            f"got {actual['quantity']}; counter_debug="
                            f"{json.dumps(actual.get('counter_debug'), ensure_ascii=False)}",
                    )

                self.assertFalse(unmatched, f"{case['id']}: some detected crops were not matched")

                # Locked/dimmed cards are labelled too, but must not enter inventory.
                for unowned in expected_unowned:
                    for actual in detected:
                        overlap = _iou(unowned["box"], actual["box"])
                        self.assertLess(
                            overlap, MIN_IOU,
                            msg=f"{case['id']}: unowned card {unowned.get('card_name')} "
                                f"was included as inventory crop {actual['box']} (IoU={overlap:.3f})",
                        )


    def test_verified_screenshot_card_identities_match_catalog(self):
        verified_cases = [
            case for case in self.cases
            if any(card.get("identity_verified") is True and card.get("state", "owned") == "owned" for card in case.get("cards", []))
        ]
        if not verified_cases:
            self.skipTest(
                "No screenshot cards have manually verified catalogue IDs yet; "
                "confirm IDs in the admin screenshot-fixture panel"
            )
        catalog = main.load_db()
        for case in verified_cases:
            with self.subTest(case_id=case.get("id")):
                with Image.open(ROOT / case["image_path"]) as source:
                    screenshot = source.convert("RGB")
                detected = scan_screenshot(screenshot, save_previews=False)["cards"]
                matched = main._enrich_admin_scan(detected, catalog)
                available = set(range(len(detected)))
                for expected in case.get("cards", []):
                    if expected.get("identity_verified") is not True or expected.get("state", "owned") != "owned":
                        continue
                    ex, ey, ew, eh = expected["box"]
                    best_index, best_iou = None, 0.0
                    for index in available:
                        ax, ay, aw, ah = detected[index]["box"]
                        ix, iy = max(ex, ax), max(ey, ay)
                        iw = max(0, min(ex + ew, ax + aw) - ix)
                        ih = max(0, min(ey + eh, ay + ah) - iy)
                        inter = iw * ih
                        union = ew * eh + aw * ah - inter
                        score = inter / union if union else 0.0
                        if score > best_iou:
                            best_index, best_iou = index, score
                    label = expected.get("card_name") or expected.get("card_id") or case["id"]
                    self.assertIsNotNone(best_index, f"{label}: no detected card crop")
                    self.assertGreaterEqual(best_iou, MIN_IOU, f"{label}: detected crop does not overlap its label")
                    available.remove(best_index)
                    expected_id = str(expected.get("card_id") or "").strip()
                    actual = matched[best_index]
                    actual_id = str(actual.get("id") or "").strip() if actual.get("matched") else ""
                    self.assertEqual(
                        expected_id, actual_id,
                        f"{label}: expected card ID {expected_id or '<unknown>'}, got {actual_id or '<unmatched>'}",
                    )


if __name__ == "__main__":
    unittest.main()
