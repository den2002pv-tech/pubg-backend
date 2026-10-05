from __future__ import annotations

import io
import math
import time
import uuid
from pathlib import Path
from typing import Any

import cv2
import imagehash
import numpy as np
from PIL import Image, ImageDraw, ImageFont


TEMP_DIR = Path("temp_images")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

NORMALIZED_SIZE = (224, 320)
MATCH_THRESHOLD = 18.0


def load_image(source: Any) -> np.ndarray:
    """Load an image as BGR numpy array."""
    if isinstance(source, np.ndarray):
        if source.ndim == 2:
            return cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
        return source.copy()

    if isinstance(source, (bytes, bytearray)):
        arr = np.frombuffer(source, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Could not decode image bytes")
        return image

    if isinstance(source, (str, Path)):
        image = cv2.imread(str(source))
        if image is None:
            raise ValueError(f"Could not read image: {source}")
        return image

    raise TypeError("Unsupported image source")


def _candidate_rectangles(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find likely card rectangles using contours and morphology."""
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    e1 = cv2.Canny(gray, 50, 150)
    e2 = cv2.Canny(gray, 80, 200)
    edges = cv2.bitwise_or(e1, e2)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(
        edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    result: list[tuple[int, int, int, int]] = []

    min_w = w * 0.06
    max_w = w * 0.45
    min_h = h * 0.18
    max_h = h * 0.75
    min_area = w * h * 0.012

    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        area = cw * ch

        if cw < min_w or cw > max_w:
            continue
        if ch < min_h or ch > max_h:
            continue
        if area < min_area:
            continue

        ratio = cw / float(ch)
        if not 0.45 <= ratio <= 0.90:
            continue

        result.append((x, y, cw, ch))

    return result


def _build_height_clusters(
    boxes: list[tuple[int, int, int, int]],
) -> list[list[tuple[int, int, int, int]]]:
    if not boxes:
        return []

    boxes = sorted(boxes, key=lambda b: b[3])
    clusters: list[list[tuple[int, int, int, int]]] = []

    for box in boxes:
        h = box[3]
        placed = False

        for cluster in clusters:
            median_h = float(np.median([b[3] for b in cluster]))
            if abs(h - median_h) / max(median_h, 1) <= 0.16:
                cluster.append(box)
                placed = True
                break

        if not placed:
            clusters.append([box])

    return clusters


def _select_main_card_family(
    boxes: list[tuple[int, int, int, int]],
    image_shape: tuple[int, int, int],
) -> list[tuple[int, int, int, int]]:
    """Prefer the repeated large card family and reject top/sidebar cards."""
    if not boxes:
        return []

    clusters = _build_height_clusters(boxes)
    h, _ = image_shape[:2]

    def score(cluster: list[tuple[int, int, int, int]]) -> float:
        count = len(cluster)
        median_y = float(np.median([b[1] + b[3] / 2 for b in cluster]))
        median_h = float(np.median([b[3] for b in cluster]))

        # Repeated cards are strongly preferred. Slightly prefer lower,
        # larger cards, which is typical for the inventory grid.
        return count * 10000 + median_y * 2 + median_h

    selected = max(clusters, key=score)

    # On noisy screenshots, a single giant false positive should not win.
    if len(selected) == 1 and len(clusters) > 1:
        alternatives = sorted(clusters, key=score, reverse=True)
        if len(alternatives[1]) >= 2:
            selected = alternatives[1]

    return selected


def _group_into_rows(
    boxes: list[tuple[int, int, int, int]],
) -> list[list[tuple[int, int, int, int]]]:
    if not boxes:
        return []

    median_h = float(np.median([b[3] for b in boxes]))
    tolerance = median_h * 0.30

    rows: list[list[tuple[int, int, int, int]]] = []

    for box in sorted(boxes, key=lambda b: b[1] + b[3] / 2):
        cy = box[1] + box[3] / 2
        target = None

        for row in rows:
            row_cy = float(np.mean([b[1] + b[3] / 2 for b in row]))
            if abs(cy - row_cy) <= tolerance:
                target = row
                break

        if target is None:
            rows.append([box])
        else:
            target.append(box)

    return [
        sorted(row, key=lambda b: b[0])
        for row in sorted(rows, key=lambda r: np.mean([b[1] for b in r]))
    ]


def _estimate_grid_step(
    centers: list[float],
    typical_size: float,
) -> float:
    if len(centers) < 2:
        return typical_size

    centers = sorted(centers)
    gaps = [b - a for a, b in zip(centers, centers[1:]) if b - a > 5]

    if not gaps:
        return typical_size

    candidates = []
    for gap in gaps:
        n = max(1, round(gap / typical_size))
        candidates.append(gap / n)

    step = float(np.median(candidates))

    if step < typical_size * 0.55:
        step = typical_size
    if step > typical_size * 1.45:
        step = typical_size

    return step


def _build_lattice_centers(
    boxes: list[tuple[int, int, int, int]],
) -> list[float]:
    """Reconstruct regular card centers, including a missing contour between cards."""
    if len(boxes) <= 1:
        return [boxes[0][0] + boxes[0][2] / 2] if boxes else []

    centers = sorted([b[0] + b[2] / 2 for b in boxes])
    typical_w = float(np.median([b[2] for b in boxes]))
    step = _estimate_grid_step(centers, typical_w)

    best = None

    for anchor in centers:
        indices = [round((c - anchor) / step) for c in centers]
        residual = sum(
            abs(c - (anchor + idx * step)) for c, idx in zip(centers, indices)
        )
        if best is None or residual < best[0]:
            best = (residual, anchor, indices)

    _, anchor, indices = best
    min_idx = min(indices)
    max_idx = max(indices)

    if max_idx - min_idx > 11:
        return centers

    lattice = [anchor + idx * step for idx in range(min_idx, max_idx + 1)]
    return lattice


def detect_inventory_cards(source: Any) -> list[list[tuple[int, int, int, int]]]:
    """Detect the main inventory card grid."""
    image = load_image(source)
    h, w = image.shape[:2]

    candidates = _candidate_rectangles(image)
    family = _select_main_card_family(candidates, image.shape)

    if not family:
        return []

    rows = _group_into_rows(family)
    output: list[list[tuple[int, int, int, int]]] = []

    for row in rows:
        if not row:
            continue

        median_w = int(np.median([b[2] for b in row]))
        median_h = int(np.median([b[3] for b in row]))
        centers = _build_lattice_centers(row)

        row_boxes = []
        for cx in centers:
            x = int(round(cx - median_w / 2))
            cy = int(round(np.mean([b[1] + b[3] / 2 for b in row])))
            y = int(round(cy - median_h / 2))

            x = max(0, min(x, w - 1))
            y = max(0, min(y, h - 1))
            cw = min(median_w, w - x)
            ch = min(median_h, h - y)

            if cw > 20 and ch > 40:
                row_boxes.append((x, y, cw, ch))

        output.append(row_boxes)

    return output


def normalize_card(image: np.ndarray) -> Image.Image:
    """Normalize a BGR card crop to a fixed RGB size."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    return pil.resize(NORMALIZED_SIZE, Image.Resampling.LANCZOS)


def extract_visual_area(card: Image.Image) -> Image.Image:
    """Keep the visual art area and remove quantity/name/UI regions."""
    w, h = card.size
    crop = card.crop(
        (
            int(w * 0.05),
            int(h * 0.05),
            int(w * 0.95),
            int(h * 0.72),
        )
    )
    return crop


def calculate_card_hashes(card: Image.Image) -> dict[str, str]:
    visual = extract_visual_area(card)

    return {
        "visual_phash": str(imagehash.phash(visual)),
        "visual_dhash": str(imagehash.dhash(visual)),
        "full_phash": str(imagehash.phash(card)),
        "full_dhash": str(imagehash.dhash(card)),
    }


def _hash_distance(a: str, b: str) -> int:
    try:
        return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))
    except Exception:
        return 999


def compare_card_hashes(
    scan_hashes: dict[str, str],
    stored: dict[str, Any] | str,
) -> float:
    """Return a weighted perceptual hash distance."""
    if isinstance(stored, str):
        # Legacy database entries used one dHash.
        return float(_hash_distance(scan_hashes.get("full_dhash", ""), stored))

    weights = {
        "visual_phash": 0.55,
        "visual_dhash": 0.25,
        "full_phash": 0.15,
        "full_dhash": 0.05,
    }

    total = 0.0
    weight_sum = 0.0

    for key, weight in weights.items():
        if key in scan_hashes and key in stored:
            total += _hash_distance(scan_hashes[key], stored[key]) * weight
            weight_sum += weight

    if weight_sum == 0:
        return 999.0

    return total / weight_sum


def find_best_match(
    scan_hashes: dict[str, str],
    database: dict[str, Any],
    threshold: float = MATCH_THRESHOLD,
) -> dict[str, Any]:
    best_id = None
    best_name = None
    best_distance = 999.0

    for card_id, entry in database.items():
        distance = compare_card_hashes(scan_hashes, entry)

        if distance < best_distance:
            best_distance = distance
            best_id = card_id
            best_name = entry.get("name", card_id) if isinstance(entry, dict) else card_id

    matched = best_id is not None and best_distance <= threshold

    return {
        "matched": matched,
        "id": best_id if matched else None,
        "name": best_name if matched else None,
        "distance": round(best_distance, 2),
    }


def _unique_name(prefix: str, extension: str = ".jpg") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{extension}"


def save_preview(card: Image.Image, row: int, col: int) -> str:
    filename = _unique_name(f"card_r{row}_c{col}")
    path = TEMP_DIR / filename
    card.save(path, quality=92)
    return f"/temp_images/{filename}"


def save_debug_image(
    source: np.ndarray,
    rows: list[list[tuple[int, int, int, int]]],
) -> str:
    image = source.copy()
    pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)

    for row_idx, row in enumerate(rows):
        for col_idx, (x, y, w, h) in enumerate(row):
            draw.rectangle((x, y, x + w, y + h), outline=(0, 255, 0), width=4)
            draw.text((x + 6, y + 6), f"{row_idx + 1}:{col_idx + 1}", fill=(255, 255, 0))

    filename = _unique_name("debug")
    path = TEMP_DIR / filename
    pil.save(path, quality=90)
    return f"/temp_images/{filename}"


def scan_screenshot(
    source: Any,
    save_previews: bool = False,
    save_debug: bool = False,
) -> dict[str, Any]:
    image = load_image(source)
    rows = detect_inventory_cards(image)

    cards = []

    for row_idx, row in enumerate(rows):
        for col_idx, (x, y, w, h) in enumerate(row):
            crop = image[y:y + h, x:x + w]
            normalized = normalize_card(crop)
            hashes = calculate_card_hashes(normalized)

            item = {
                "row": row_idx,
                "col": col_idx,
                "box": [x, y, w, h],
                "width": w,
                "height": h,
                "hashes": hashes,
            }

            if save_previews:
                item["preview"] = save_preview(normalized, row_idx, col_idx)

            cards.append(item)

    result = {
        "cards": cards,
        "count": len(cards),
    }

    if save_debug:
        result["debug"] = save_debug_image(image, rows)

    return result


def match_scan_results(
    scan_result: dict[str, Any],
    database: dict[str, Any],
) -> dict[str, Any]:
    cards = []

    for card in scan_result.get("cards", []):
        match = find_best_match(card["hashes"], database)
        cards.append({**card, "match": match})

    return {
        **scan_result,
        "cards": cards,
    }
