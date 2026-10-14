"""Visual feature extraction for manually labelled owned/unowned card crops.

Features are descriptive measurements only; they do not make ownership decisions.
Keep feature versioned so future experiments can compare compatible samples.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image, ImageStat

FEATURE_VERSION = 1


def _region_stats(image: Image.Image, prefix: str) -> dict[str, float]:
    gray = image.convert("L")
    hist = gray.histogram()
    total = sum(hist) or 1
    midpoint = total / 2
    cumulative = 0
    median = 0
    for value, count in enumerate(hist):
        cumulative += count
        if cumulative >= midpoint:
            median = value
            break
    stats = ImageStat.Stat(gray)
    return {
        f"{prefix}_luminance_mean": round(float(stats.mean[0]), 3),
        f"{prefix}_luminance_median": float(median),
        f"{prefix}_luminance_stddev": round(float(stats.stddev[0]), 3),
    }


def extract_card_visual_features(image: Image.Image) -> dict[str, Any]:
    """Return reproducible luminance, saturation and region features for a card."""
    card = image.convert("RGB")
    if card.width < 2 or card.height < 2:
        raise ValueError("Card crop must be at least 2x2 pixels")

    # Normalize feature extraction cost, not the saved original image.
    card = card.resize((96, 144), Image.Resampling.BILINEAR)
    rgb = np.asarray(card, dtype=np.uint8)
    hsv = np.asarray(card.convert("HSV"), dtype=np.uint8)
    luminance = np.asarray(card.convert("L"), dtype=np.uint8)
    saturation = hsv[:, :, 1].astype(np.float32)
    height, width = luminance.shape

    frame_mask = np.zeros((height, width), dtype=bool)
    frame_x = max(1, int(width * 0.10))
    frame_y = max(1, int(height * 0.08))
    frame_mask[:frame_y, :] = True
    frame_mask[-frame_y:, :] = True
    frame_mask[:, :frame_x] = True
    frame_mask[:, -frame_x:] = True

    features: dict[str, Any] = {
        "version": FEATURE_VERSION,
        "width": int(image.width),
        "height": int(image.height),
        "luminance_mean": round(float(luminance.mean()), 3),
        "luminance_median": float(np.median(luminance)),
        "luminance_stddev": round(float(luminance.std()), 3),
        "saturation_mean": round(float(saturation.mean()), 3),
        "saturation_median": float(np.median(saturation)),
        "saturated_pixel_ratio": round(float(np.mean(saturation >= 70)), 4),
        "frame_luminance_mean": round(float(luminance[frame_mask].mean()), 3),
        "frame_saturation_mean": round(float(saturation[frame_mask].mean()), 3),
    }

    regions = {
        "center": card.crop((int(card.width * .12), int(card.height * .16),
                             int(card.width * .88), int(card.height * .78))),
        "title": card.crop((0, 0, card.width, max(2, int(card.height * .16)))),
        "bottom": card.crop((0, int(card.height * .78), card.width, card.height)),
    }
    for name, region in regions.items():
        features.update(_region_stats(region, name))
        features[f"{name}_saturation_mean"] = round(
            float(np.asarray(region.convert("HSV"), dtype=np.uint8)[:, :, 1].mean()), 3
        )

    return features


def best_feature_splits(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Find exploratory one-feature thresholds; never treat training score as validation."""
    classes = {
        "owned": [s for s in samples if s.get("state") == "owned"],
        "unowned": [s for s in samples if s.get("state") == "unowned"],
    }
    owned_count, unowned_count = len(classes["owned"]), len(classes["unowned"])
    if not owned_count or not unowned_count:
        return {
            "ready": False,
            "owned_count": owned_count,
            "unowned_count": unowned_count,
            "message": "Нужны размеченные примеры обоих состояний.",
            "features": [],
        }

    ignored = {"version", "width", "height"}
    keys = sorted({
        key
        for sample in samples
        for key, value in sample.get("features", {}).items()
        if key not in ignored and isinstance(value, (int, float))
    })
    rows = []
    for key in keys:
        values = [
            (float(sample["features"][key]), sample["state"])
            for sample in samples
            if isinstance(sample.get("features", {}).get(key), (int, float))
        ]
        unique = sorted({value for value, _ in values})
        if len(unique) < 2:
            continue
        thresholds = [(a + b) / 2 for a, b in zip(unique, unique[1:])]
        best = None
        for threshold in thresholds:
            for direction in ("lower_is_unowned", "higher_is_unowned"):
                tp = tn = fp = fn = 0
                for value, state in values:
                    predicted = (
                        "unowned" if value <= threshold else "owned"
                    ) if direction == "lower_is_unowned" else (
                        "unowned" if value >= threshold else "owned"
                    )
                    if state == "unowned" and predicted == "unowned":
                        tp += 1
                    elif state == "owned" and predicted == "owned":
                        tn += 1
                    elif state == "owned":
                        fp += 1
                    else:
                        fn += 1
                sensitivity = tp / max(1, tp + fn)
                specificity = tn / max(1, tn + fp)
                balanced = (sensitivity + specificity) / 2
                candidate = {
                    "feature": key,
                    "threshold": round(threshold, 4),
                    "direction": direction,
                    "balanced_accuracy": round(balanced, 4),
                    "accuracy": round((tp + tn) / max(1, tp + tn + fp + fn), 4),
                    "owned_false_exclusions": fp,
                    "unowned_missed": fn,
                    "samples": tp + tn + fp + fn,
                }
                if best is None or candidate["balanced_accuracy"] > best["balanced_accuracy"]:
                    best = candidate
        if best:
            rows.append(best)
    rows.sort(key=lambda row: (row["balanced_accuracy"], row["samples"]), reverse=True)
    return {
        "ready": True,
        "owned_count": owned_count,
        "unowned_count": unowned_count,
        "message": "Порог подобран на тех же эталонах; это исследовательская оценка, не независимая проверка.",
        "features": rows[:12],
    }
