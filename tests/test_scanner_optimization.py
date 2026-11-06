"""Exact compatibility oracles captured before scanner performance changes."""
import json
from pathlib import Path

import imagehash
import numpy as np
import pytest
from PIL import Image

import scanner
from scanner import _image_from_any, _digit_glyph_features, _COUNTER_TEMPLATE_BITS

def reference_frame(card: Image.Image) -> Image.Image:
    card = _image_from_any(card)

    arr = np.asarray(card)
    active = arr.sum(axis=2, dtype=np.uint16) > 24

    if active.any():
        ys, xs = np.where(active)
        x1, x2 = int(xs.min()), int(xs.max()) + 1
        y1, y2 = int(ys.min()), int(ys.max()) + 1

        if (
            x1 > card.width * 0.02
            or y1 > card.height * 0.02
            or x2 < card.width * 0.98
            or y2 < card.height * 0.98
        ):
            card = card.crop((x1, y1, x2, y2))

    w, h = card.size
    top_h = max(2, int(h * 0.10))
    bottom_h = max(2, int(h * 0.10))
    side_w = max(2, int(w * 0.10))

    top = card.crop((0, 0, w, top_h))
    bottom = card.crop((0, h - bottom_h, w, h - 1))
    left = card.crop((0, top_h, side_w, max(top_h + 1, h - bottom_h)))
    right = card.crop((w - side_w, top_h, w, max(top_h + 1, h - bottom_h)))

    target_w = w
    target_h = max(1, int(target_w * 0.18))
    parts = [
        top.resize((target_w, target_h), Image.Resampling.BILINEAR),
        bottom.resize((target_w, target_h), Image.Resampling.BILINEAR),
        left.resize((target_w, target_h), Image.Resampling.BILINEAR),
        right.resize((target_w, target_h), Image.Resampling.BILINEAR),
    ]

    canvas = Image.new("RGB", (target_w, target_h * len(parts)))
    for index, part in enumerate(parts):
        canvas.paste(part, (0, index * target_h))
    return canvas

def reference_digit_score(mask: np.ndarray, digit: int, features: dict[str, Any] | None = None) -> float:
    if features is None:
        features = _digit_glyph_features(mask)
    if features is None:
        return 0.0
    candidate = features["candidate"]
    candidate_total = features["candidate_total"]
    hole_count = features["hole_count"]
    aspect = features["aspect"]

    best = 0.0
    for target, target_total in _COUNTER_TEMPLATE_BITS[digit]:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                sy1, sy2 = max(0, -dy), min(20, 20 - dy)
                dy1, dy2 = max(0, dy), min(20, 20 + dy)
                sx1, sx2 = max(0, -dx), min(9, 9 - dx)
                dx1, dx2 = max(0, dx), min(9, 9 + dx)
                inter = np.logical_and(candidate[sy1:sy2, sx1:sx2], target[dy1:dy2, dx1:dx2]).sum()
                union = candidate_total + target_total - inter
                iou = float(inter / union) if union else 0.0
                best = max(best, iou)

    # PUBG's counter font uses a closed-top 4 in some crops. Its triangular
    # counter can form one enclosed background component, just like a 6.
    # Treating every 4 with one hole as impossible suppresses the correct 4 score
    # and makes low-resolution ×4 badges look like ×6.
    allowed_holes = {
        0: {1}, 1: {0}, 2: {0}, 3: {0}, 4: {0, 1},
        5: {0}, 6: {1}, 7: {0}, 8: {2}, 9: {1},
    }[digit]
    if hole_count not in allowed_holes:
        best *= 0.30

    # Resizing every glyph to the same 9x20 canvas discards its original
    # aspect ratio. That makes a wide 0 (or 6) look deceptively similar to a
    # narrow 1, especially in low-resolution crops. Keep the original ratio
    # as a weak prior rather than letting it override the actual glyph shape.
    if digit == 1 and aspect > 0.38:
        best *= max(0.25, 1.0 - (aspect - 0.38) * 2.0)
    elif digit in (0, 6, 8, 9) and aspect < 0.24:
        best *= max(0.25, aspect / 0.24)

    return best

def reference_hashes(card):
    card = _image_from_any(card)
    visual = scanner.extract_visual_area(card)
    return {
        "full_phash": str(imagehash.phash(card)),
        "full_dhash": str(imagehash.dhash(card)),
        "visual_phash": str(imagehash.phash(visual)),
        "visual_dhash": str(imagehash.dhash(visual)),
        "frame_colorhash": str(imagehash.colorhash(reference_frame(card))),
    }


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
def test_hash_and_frame_parity(mode):
    rng = np.random.default_rng(42)
    for shape in [(31, 23, 3), (384, 256, 3), (721, 483, 3)]:
        arr = rng.integers(0, 256, shape, dtype=np.uint8)
        for border in (False, True):
            if border:
                arr[:20] = 0
                arr[-10:] = 0
                arr[:, :8] = 0
            card = Image.fromarray(arr).convert(mode)
            assert scanner.extract_frame_area(card).tobytes() == reference_frame(card).tobytes()
            assert scanner.calculate_card_hashes(card) == reference_hashes(card)
    for value in (0, 8, 9, 255):
        card = Image.new(mode, (30, 40), value)
        assert scanner.calculate_card_hashes(card) == reference_hashes(card)


def test_raw_and_badge_cleaned_fixture_hash_parity():
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "tests/fixtures/counter/cases.json").read_text())["cases"]
    cleaned_count = 0
    for case in cases:
        with Image.open(root / case["image_path"]) as source:
            raw = source.convert("RGB")
        clean, *_ = scanner._detect_counter(raw)
        cleaned_count += clean.tobytes() != raw.tobytes()
        for card in (raw, clean):
            assert scanner.calculate_card_hashes(card) == reference_hashes(card), case["id"]
    assert cleaned_count > 0


def test_digit_scores_exact_parity():
    rng = np.random.default_rng(42)
    masks = [rng.integers(0, 2, (h, w), dtype=np.uint8) * 255
             for h, w in [(3, 3), (20, 9), (50, 32), (30, 5)] for _ in range(20)]
    masks += [target.astype(np.uint8) * 255 for variants in _COUNTER_TEMPLATE_BITS.values()
              for target, _ in variants]
    masks += [np.zeros((20, 9), dtype=np.uint8), np.full((20, 9), 255, dtype=np.uint8)]
    for mask in masks:
        features = _digit_glyph_features(mask)
        for digit in range(10):
            assert scanner._digit_score(mask, digit, features) == reference_digit_score(mask, digit, features)
