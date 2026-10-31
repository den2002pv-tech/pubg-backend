"""Verify the refactored counter primitives are bit-identical to the old code."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import scanner  # noqa: E402


def old_digit_score(mask: np.ndarray, digit: int) -> float:
    """Verbatim copy of the pre-optimization implementation."""
    if mask is None or mask.size == 0 or mask.shape[0] < 3 or mask.shape[1] < 3:
        return 0.0
    normalized = cv2.resize(mask.astype(np.uint8), (9, 20), interpolation=cv2.INTER_AREA)
    candidate = normalized >= 128
    padded = np.pad(candidate.astype(np.uint8), 1, constant_values=0)
    background = (padded == 0).astype(np.uint8)
    _, component_labels = cv2.connectedComponents(background, connectivity=8)
    exterior_label = int(component_labels[0, 0])
    background_labels = np.unique(component_labels[background > 0])
    hole_count = sum(int(label) != exterior_label for label in background_labels)
    templates = [scanner._COUNTER_TEMPLATES[digit]] + scanner._COUNTER_VARIANTS.get(digit, [])
    best = 0.0
    for tmpl_str in templates:
        target = scanner._template_image(tmpl_str) > 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                sy1, sy2 = max(0, -dy), min(20, 20 - dy)
                dy1, dy2 = max(0, dy), min(20, 20 + dy)
                sx1, sx2 = max(0, -dx), min(9, 9 - dx)
                dx1, dx2 = max(0, dx), min(9, 9 + dx)
                inter = np.logical_and(candidate[sy1:sy2, sx1:sx2], target[dy1:dy2, dx1:dx2]).sum()
                union = candidate.sum() + target.sum() - inter
                iou = float(inter / union) if union else 0.0
                best = max(best, iou)
    allowed_holes = {
        0: {1}, 1: {0}, 2: {0}, 3: {0}, 4: {0, 1},
        5: {0}, 6: {1}, 7: {0}, 8: {2}, 9: {1},
    }[digit]
    if hole_count not in allowed_holes:
        best *= 0.30
    aspect = mask.shape[1] / float(mask.shape[0])
    if digit == 1 and aspect > 0.38:
        best *= max(0.25, 1.0 - (aspect - 0.38) * 2.0)
    elif digit in (0, 6, 8, 9) and aspect < 0.24:
        best *= max(0.25, aspect / 0.24)
    return best


def old_multiply_score(mask: np.ndarray) -> float:
    """Verbatim copy of the pre-optimization implementation."""
    if mask is None or mask.size == 0 or mask.shape[0] < 3 or mask.shape[1] < 3:
        return 0.0
    normalized = cv2.resize(mask.astype(np.uint8), (15, 15), interpolation=cv2.INTER_AREA)
    candidate = normalized >= 128
    fill = float(candidate.mean())
    if not (0.035 <= fill <= 0.80):
        return 0.0
    corners = (
        candidate[0:4, 0:4].any(),
        candidate[0:4, 11:15].any(),
        candidate[11:15, 0:4].any(),
        candidate[11:15, 11:15].any(),
    )
    if sum(corners) < 3:
        return 0.0
    templates = []
    thin = np.zeros((15, 15), dtype=np.uint8)
    cv2.line(thin, (3, 3), (11, 11), 255, 2)
    cv2.line(thin, (11, 3), (3, 11), 255, 2)
    templates.append(thin > 0)
    thick = np.zeros((15, 15), dtype=np.uint8)
    cv2.line(thick, (2, 2), (12, 12), 255, 3)
    cv2.line(thick, (12, 2), (2, 12), 255, 3)
    templates.append(thick > 0)
    best = 0.0
    for target in templates:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                sy1, sy2 = max(0, -dy), min(15, 15 - dy)
                dy1, dy2 = max(0, dy), min(15, 15 + dy)
                sx1, sx2 = max(0, -dx), min(15, 15 - dx)
                dx1, dx2 = max(0, dx), min(15, 15 + dx)
                inter = np.logical_and(candidate[sy1:sy2, sx1:sx2], target[dy1:dy2, dx1:dx2]).sum()
                union = candidate.sum() + target.sum() - inter
                best = max(best, float(inter / union) if union else 0.0)
    return best


def main() -> None:
    rng = np.random.default_rng(42)
    max_diff = 0.0
    n_checked = 0
    for size in ((4, 4), (5, 10), (8, 20), (12, 6), (20, 12), (30, 15), (40, 20)):
        h, w = size
        for _ in range(40):
            mask = (rng.random((h, w)) < 0.45).astype(np.uint8) * 255
            for digit in range(10):
                a = old_digit_score(mask, digit)
                b = scanner._digit_score(mask, digit)
                diff = abs(a - b)
                max_diff = max(max_diff, diff)
                n_checked += 1
                if a != b:
                    print(f"DIFF digit={digit} size={size} old={a!r} new={b!r}")
    print(f"digit_score: {n_checked} checks, max abs diff = {max_diff}")

    # multiply: random blobs + real X shapes
    max_diff = 0.0
    for _ in range(60):
        mask = (rng.random((20, 20)) < 0.3).astype(np.uint8) * 255
        a = old_multiply_score(mask)
        b = scanner._multiply_score(mask)
        max_diff = max(max_diff, abs(a - b))
        if a != b:
            print(f"DIFF multiply old={a!r} new={b!r}")
    for thickness, (p1, p2) in ((2, ((3, 3), (11, 11))), (3, ((2, 2), (12, 12)))):
        canvas = np.zeros((20, 20), dtype=np.uint8)
        cv2.line(canvas, (p1[0] + 5, p1[1] + 5), (p2[0] + 5, p2[1] + 5), 255, thickness)
        cv2.line(canvas, (p2[0] + 5, p1[1] + 5), (p1[0] + 5, p2[1] + 5), 255, thickness)
        a = old_multiply_score(canvas)
        b = scanner._multiply_score(canvas)
        print(f"X shape t={thickness}: old={a!r} new={b!r} equal={a == b}")
        max_diff = max(max_diff, abs(a - b))
    print(f"multiply_score: max abs diff = {max_diff}")

    # timing: per-contour digit scoring old vs new
    mask = (rng.random((40, 20)) < 0.5).astype(np.uint8) * 255
    t0 = time.perf_counter()
    for _ in range(50):
        for d in range(10):
            old_digit_score(mask, d)
    old_ms = (time.perf_counter() - t0) / 50 * 1000
    t0 = time.perf_counter()
    for _ in range(50):
        feats = scanner._digit_glyph_features(mask)
        for d in range(10):
            scanner._digit_score(mask, d, feats)
    new_ms = (time.perf_counter() - t0) / 50 * 1000
    print(f"per-contour 10 digits: old={old_ms:.2f} ms new={new_ms:.2f} ms "
          f"speedup={old_ms / new_ms:.1f}x")


if __name__ == "__main__":
    main()
