"""Regression tests for real, manually labelled card-counter fixtures.

Run with: python -m unittest discover -s tests
Every saved case image is the exact card crop passed to scanner._detect_counter.
A mismatch is intentionally a failing regression test until the detector is fixed.
"""
import json
import unittest
from pathlib import Path

from PIL import Image

from scanner import _detect_counter

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "counter" / "cases.json"


class CounterFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MANIFEST.exists():
            cls.cases = []
            return
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.cases = payload.get("cases", [])

    def test_has_real_labeled_cases(self):
        if not self.cases:
            self.skipTest("No manually labelled counter fixtures have been saved yet")
        self.assertGreater(len(self.cases), 0)

    def test_real_counter_fixtures_match_expected_quantities(self):
        if not self.cases:
            self.skipTest("No manually labelled counter fixtures have been saved yet")
        for case in self.cases:
            with self.subTest(case_id=case.get("id"), card=case.get("card_name")):
                image_path = ROOT / case["image_path"]
                self.assertTrue(image_path.is_file(), f"Missing fixture image: {image_path}")
                with Image.open(image_path) as source:
                    image = source.convert("RGB")
                _, actual, confidence, debug = _detect_counter(image)
                self.assertEqual(
                    case["expected_quantity"],
                    actual,
                    msg=(
                        f"{case.get('card_name') or case.get('id')}: expected "
                        f"{case['expected_quantity']}, got {actual}; "
                        f"confidence={confidence}; debug={json.dumps(debug, ensure_ascii=False)}"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
