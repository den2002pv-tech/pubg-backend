"""Regression tests for visual feature extraction and exploratory state analysis."""
import unittest
from PIL import Image, ImageDraw, ImageEnhance

from card_state_features import best_feature_splits, extract_card_visual_features


def _sample_card() -> Image.Image:
    image = Image.new("RGB", (120, 180), (190, 135, 60))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, 112, 172), outline=(245, 205, 70), width=6)
    draw.rectangle((20, 28, 100, 120), fill=(40, 160, 90))
    draw.rectangle((10, 145, 110, 165), fill=(220, 180, 110))
    return image


class CardStateFeatureTests(unittest.TestCase):
    def test_features_capture_luminance_and_saturation_changes(self):
        normal = _sample_card()
        dimmed = ImageEnhance.Color(ImageEnhance.Brightness(normal).enhance(0.5)).enhance(0.45)
        normal_features = extract_card_visual_features(normal)
        dimmed_features = extract_card_visual_features(dimmed)

        self.assertEqual(1, normal_features["version"])
        self.assertLess(dimmed_features["luminance_mean"], normal_features["luminance_mean"])
        self.assertLess(dimmed_features["saturation_mean"], normal_features["saturation_mean"])
        self.assertIn("frame_luminance_mean", normal_features)
        self.assertIn("title_saturation_mean", normal_features)
        self.assertIn("bottom_luminance_stddev", normal_features)

    def test_analysis_finds_a_split_and_counts_both_classes(self):
        normal = extract_card_visual_features(_sample_card())
        dimmed_image = ImageEnhance.Color(ImageEnhance.Brightness(_sample_card()).enhance(0.4)).enhance(0.4)
        dimmed = extract_card_visual_features(dimmed_image)
        report = best_feature_splits(
            [{"state": "owned", "features": normal} for _ in range(3)]
            + [{"state": "unowned", "features": dimmed} for _ in range(3)]
        )

        self.assertTrue(report["ready"])
        self.assertEqual(3, report["owned_count"])
        self.assertEqual(3, report["unowned_count"])
        self.assertTrue(report["features"])
        self.assertGreaterEqual(report["features"][0]["balanced_accuracy"], 0.5)

    def test_analysis_explains_when_one_class_is_missing(self):
        report = best_feature_splits([{
            "state": "owned",
            "features": extract_card_visual_features(_sample_card()),
        }])
        self.assertFalse(report["ready"])
        self.assertEqual(1, report["owned_count"])
        self.assertEqual(0, report["unowned_count"])


if __name__ == "__main__":
    unittest.main()
