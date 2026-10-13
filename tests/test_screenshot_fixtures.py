"""End-to-end regression tests for inventory screenshots."""
import json
import unittest
from pathlib import Path

from PIL import Image

from scanner import scan_screenshot

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "screenshots" / "cases.json"
MIN_IOU = 0.70


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    intersection = max(0, right - left) * max(0, bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union else 0.0


class ScreenshotFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = (
            json.loads(MANIFEST.read_text(encoding="utf-8")).get("cases", [])
            if MANIFEST.exists() else []
        )

    def test_has_labeled_screenshot_cases(self):
        if not self.cases:
            self.skipTest("No labelled full-screenshot fixtures have been saved yet")
        self.assertGreater(len(self.cases), 0)

    def test_screenshot_contours_and_card_quantities(self):
        if not self.cases:
            self.skipTest("No labelled full-screenshot fixtures have been saved yet")
        for case in self.cases:
            with self.subTest(case_id=case.get("id"), screenshot=case.get("image_path")):
                relative = Path(case["image_path"])
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                screenshot_path = ROOT / relative
                self.assertTrue(screenshot_path.is_file(), f"Missing screenshot: {screenshot_path}")
                with Image.open(screenshot_path) as source:
                    screenshot = source.convert("RGB")
                expected_cards = case.get("cards")
                self.assertIsInstance(expected_cards, list)
                self.assertGreater(len(expected_cards), 0)

                detected = scan_screenshot(screenshot, save_previews=False)["cards"]
                self.assertEqual(
                    len(expected_cards), len(detected),
                    msg=f"Expected {len(expected_cards)} cards, detected {len(detected)}; "
                        f"boxes={[card['box'] for card in detected]}",
                )
                unmatched = list(detected)
                for expected in expected_cards:
                    expected_box = expected.get("box")
                    self.assertIsInstance(expected_box, list)
                    self.assertEqual(4, len(expected_box))
                    matches = sorted(
                        ((_iou(expected_box, item["box"]), item) for item in unmatched),
                        key=lambda pair: pair[0], reverse=True,
                    )
                    self.assertTrue(matches)
                    overlap, actual = matches[0]
                    self.assertGreaterEqual(
                        overlap, MIN_IOU,
                        msg=f"Contour mismatch for {expected.get('card_name')}: "
                            f"expected={expected_box}, detected={actual['box']}, IoU={overlap:.3f}",
                    )
                    unmatched.remove(actual)
                    expected_quantity = expected.get("expected_quantity")
                    self.assertIsInstance(expected_quantity, int)
                    self.assertGreaterEqual(expected_quantity, 1)
                    self.assertEqual(
                        expected_quantity, actual["quantity"],
                        msg=f"{expected.get('card_name')}: expected quantity {expected_quantity}, "
                            f"got {actual['quantity']}; counter_debug="
                            f"{json.dumps(actual.get('counter_debug'), ensure_ascii=False)}",
                    )


if __name__ == "__main__":
    unittest.main()
