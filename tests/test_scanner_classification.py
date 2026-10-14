"""Focused regression tests for counter geometry and dimmed-card classification."""
import numpy as np
from PIL import Image, ImageDraw

from scanner import (
    _counter_digit_aligned,
    _refine_digit_prediction,
    is_card_darkened,
)


def test_counter_digit_rejects_noise_far_below_multiply_sign():
    multiply = {"x": 165, "y": 12, "w": 10, "h": 10}
    noise_digit = {"x": 115, "y": 43, "w": 5, "h": 8}
    real_digit = {"x": 178, "y": 5, "w": 12, "h": 24}

    assert not _counter_digit_aligned(multiply, noise_digit)
    assert _counter_digit_aligned(multiply, real_digit)


def test_refine_digit_prediction_distinguishes_open_six_from_zero():
    # A normalized 6-like glyph with an open upper-right area.
    glyph = np.zeros((24, 12), dtype=np.uint8)
    cv2 = __import__("cv2")
    cv2.ellipse(glyph, (4, 14), (3, 5), 0, 30, 330, 255, 1)
    cv2.line(glyph, (4, 9), (4, 3), 255, 1)
    glyph[3:9, 5:9] = 0
    scores = {digit: 0.2 for digit in range(10)}
    scores[0] = 0.81
    scores[6] = 0.79

    digit, _ = _refine_digit_prediction(glyph, 0, scores)
    assert digit == 6


def _make_card(darkened=False, dark_art=True):
    image = Image.new("RGB", (120, 200), (25, 25, 25) if dark_art else (160, 80, 30))
    draw = ImageDraw.Draw(image)
    # White frame with a small gold accent; the artwork remains intentionally dark.
    draw.rectangle((2, 2, 117, 197), outline=(235, 235, 235), width=4)
    draw.line((4, 20, 4, 55), fill=(220, 170, 45), width=3)
    # Bright emblem background at the bottom center.
    draw.ellipse((48, 176, 72, 194), fill=(245, 245, 245))
    draw.polygon([(53, 185), (60, 179), (67, 185), (65, 190), (55, 190)], fill=(210, 155, 35))
    if darkened:
        pixels = np.asarray(image).astype(np.float32)
        gray = pixels.mean(axis=2, keepdims=True)
        pixels = gray * 0.48
        image = Image.fromarray(np.clip(np.repeat(pixels, 3, axis=2), 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(image)
        draw.rectangle((2, 2, 117, 197), outline=(120, 120, 120), width=4)
        draw.ellipse((48, 176, 72, 194), fill=(90, 90, 90))
    return image


def test_dark_art_does_not_make_an_active_card_look_locked():
    assert not is_card_darkened(_make_card(darkened=False, dark_art=True))


def test_dimmed_frame_and_badge_are_classified_as_locked():
    assert is_card_darkened(_make_card(darkened=True, dark_art=True))
