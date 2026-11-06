"""Pixel-identity regression tests for the NumPy frame-area assembly."""
import numpy as np
from PIL import Image

import scanner


def _legacy_extract_frame_area(card: Image.Image) -> Image.Image:
    """Reference implementation retained to guard frame_colorhash identity."""
    card = scanner._image_from_any(card)
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


def test_numpy_frame_area_is_pixel_identical_to_legacy_pil_pipeline():
    rng = np.random.default_rng(20261106)
    cases = [
        rng.integers(0, 256, (352, 240, 3), dtype=np.uint8),
        rng.integers(0, 256, (240, 160, 3), dtype=np.uint8),
        np.zeros((352, 240, 3), dtype=np.uint8),
    ]

    # Include an image whose active pixels trigger the historical content crop.
    cropped = np.zeros((352, 240, 3), dtype=np.uint8)
    cropped[7:345, 5:235] = rng.integers(
        1, 256, (338, 230, 3), dtype=np.uint8,
    )
    cases.append(cropped)

    for pixels in cases:
        image = Image.fromarray(pixels, mode="RGB")
        expected = np.asarray(_legacy_extract_frame_area(image))
        actual = np.asarray(scanner.extract_frame_area(image))
        assert actual.shape == expected.shape
        np.testing.assert_array_equal(actual, expected)
