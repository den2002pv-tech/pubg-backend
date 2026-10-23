"""Regression tests for card ownership filtering during screenshot scanning."""

from pathlib import Path

from PIL import Image

import scanner


def _fake_hashes(_card, _timings=None):
    return {
        "full_phash": "aaaaaaaa",
        "full_dhash": "bbbbbbbb",
        "visual_phash": "cccccccc",
        "visual_dhash": "dddddddd",
        "frame_colorhash": "eeeeeeee",
    }


def _prepare_single_card(monkeypatch):
    monkeypatch.setattr(
        scanner,
        "detect_inventory_cards",
        lambda _image: [{"row": 0, "col": 0, "box": [10, 10, 40, 70]}],
    )
    monkeypatch.setattr(scanner, "is_card_darkened", lambda _card: True)
    monkeypatch.setattr(scanner, "calculate_card_hashes", _fake_hashes)
    image = Image.new("RGB", (100, 100), (90, 90, 90))
    return image


def test_gray_card_with_counter_evidence_is_not_filtered(monkeypatch):
    image = _prepare_single_card(monkeypatch)
    monkeypatch.setattr(
        scanner,
        "_detect_counter",
        lambda card: (
            card,
            11,
            0.8,
            {"candidates": 2, "selected": 11, "reason": "ok"},
        ),
    )

    result = scanner.scan_screenshot(image, save_previews=False)

    assert result["count"] == 1
    assert result["cards"][0]["quantity"] == 11
    assert result["profile"]["geometric_detected_count"] == 1
    assert result["profile"]["darkened_filtered_count"] == 0


def test_darkened_card_without_counter_evidence_remains_excluded(monkeypatch):
    image = _prepare_single_card(monkeypatch)
    monkeypatch.setattr(
        scanner,
        "_detect_counter",
        lambda card: (
            card,
            1,
            0.0,
            {"candidates": 0, "selected": None, "reason": "no_valid_badge"},
        ),
    )

    result = scanner.scan_screenshot(image, save_previews=False)

    assert result["count"] == 0
    assert result["cards"] == []
    assert result["profile"]["geometric_detected_count"] == 1
    assert result["profile"]["darkened_filtered_count"] == 1


def test_gray_card_screenshot_without_counter_is_not_filtered():
    screenshot_path = Path(__file__).with_name("Screenshot_20261020_015450_PUBGM FLASH.png")
    with Image.open(screenshot_path) as screenshot:
        result = scanner.scan_screenshot(screenshot, save_previews=False)

    assert result["count"] == 1
    assert result["profile"]["geometric_detected_count"] == 1
    assert result["profile"]["darkened_filtered_count"] == 0


def test_scan_reuses_hashes_when_counter_crop_is_unchanged(monkeypatch):
    image = _prepare_single_card(monkeypatch)
    monkeypatch.setattr(scanner, "is_card_darkened", lambda _card: False)
    monkeypatch.setattr(
        scanner,
        "_detect_counter",
        lambda card: (card, 1, 0.0, {"reason": "no_valid_badge"}),
    )

    result = scanner.scan_screenshot(image, save_previews=False)

    assert result["count"] == 1
    assert result["cards"][0]["hashes"] == result["cards"][0]["raw_hashes"]
    assert result["profile"]["hash_calculation_count"] == 1
    assert result["profile"]["hash_operation_count"] == 5
    assert result["profile"]["raw_hash_calculation_count"] == 1
    assert result["profile"]["clean_hash_calculation_count"] == 0
    assert result["profile"]["hash_reuse_count"] == 1
    assert result["profile"]["hash_input_dimensions"] == [
        {"raw": [40, 70], "clean": [40, 70]}
    ]


def test_scan_keeps_separate_hashes_when_counter_crop_was_cleaned(monkeypatch):
    image = _prepare_single_card(monkeypatch)
    monkeypatch.setattr(
        scanner,
        "_detect_counter",
        lambda card: (card.copy(), 3, 0.9, {"reason": "ok"}),
    )

    result = scanner.scan_screenshot(image, save_previews=False)

    assert result["count"] == 1
    assert result["cards"][0]["hashes"] is not result["cards"][0]["raw_hashes"]
    assert result["profile"]["hash_calculation_count"] == 2
    assert result["profile"]["hash_operation_count"] == 10
    assert result["profile"]["raw_hash_calculation_count"] == 1
    assert result["profile"]["clean_hash_calculation_count"] == 1
    assert result["profile"]["hash_reuse_count"] == 0


def test_rgb_inputs_are_not_copied_during_mode_normalization():
    image = Image.new("RGB", (20, 30), (10, 20, 30))
    assert scanner._image_from_any(image) is image

    rgba = image.convert("RGBA")
    converted = scanner._image_from_any(rgba)
    assert converted.mode == "RGB"
    assert converted is not rgba


def test_hash_timing_breakdown_covers_normalization_and_hash_operations():
    image = Image.new("RGB", (60, 100), (40, 50, 60))
    timings = {}

    hashes = scanner.calculate_card_hashes(image, timings)

    assert len(hashes) == 5
    assert set(timings) == {
        "hash_normalization_ms",
        "hash_frame_extract_ms",
        "hash_compute_ms",
    }
    assert all(value >= 0 for value in timings.values())
