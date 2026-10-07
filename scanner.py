from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import cv2
import imagehash
import numpy as np
from PIL import Image

TEMP_DIR = Path("temp_images")
NORMALIZED_SIZE = (224, 320)


def _image_from_any(value: Image.Image | bytes) -> Image.Image:
    if isinstance(value, Image.Image):
        return value.convert("RGB")
    from io import BytesIO
    return Image.open(BytesIO(value)).convert("RGB")


def _candidate_rectangles(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 45, 140)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        ratio = cw / float(ch or 1)
        area = cw * ch
        if cw < w * 0.055 or cw > w * 0.45:
            continue
        if ch < h * 0.18 or ch > h * 0.78:
            continue
        if not 0.45 <= ratio <= 0.90:
            continue
        if area < w * h * 0.012:
            continue
        out.append((x, y, cw, ch))
    return out


def _cluster_rows(rects: list[tuple[int, int, int, int]], image_h: int) -> list[list[tuple[int, int, int, int]]]:
    rows: list[list[tuple[int, int, int, int]]] = []
    for rect in sorted(rects, key=lambda r: r[1] + r[3] / 2):
        cy = rect[1] + rect[3] / 2
        placed = False
        for row in rows:
            mean_cy = np.mean([r[1] + r[3] / 2 for r in row])
            if abs(cy - mean_cy) <= image_h * 0.09:
                row.append(rect)
                placed = True
                break
        if not placed:
            rows.append([rect])
    return [sorted(row, key=lambda r: r[0]) for row in rows if len(row) >= 2]


def _edge_score(gray_edges: np.ndarray, rect: tuple[int, int, int, int]) -> float:
    x, y, w, h = rect
    ih, iw = gray_edges.shape[:2]
    x = max(0, min(iw - 1, x)); y = max(0, min(ih - 1, y))
    x2 = max(x + 1, min(iw, x + w)); y2 = max(y + 1, min(ih, y + h))
    bw = max(2, int((x2 - x) * 0.035))
    bh = max(2, int((y2 - y) * 0.035))
    strips = [
        gray_edges[y:y2, x:x + bw],
        gray_edges[y:y2, max(x, x2 - bw):x2],
        gray_edges[y:y + bh, x:x2],
        gray_edges[max(y, y2 - bh):y2, x:x2],
    ]
    vals = [s.mean() for s in strips if s.size]
    return float(np.mean(vals)) if vals else 0.0


def _complete_row(row: list[tuple[int, int, int, int]], img: np.ndarray) -> list[tuple[int, int, int, int]]:
    if len(row) < 2:
        return row
    h, w = img.shape[:2]
    widths = np.array([r[2] for r in row], dtype=float)
    heights = np.array([r[3] for r in row], dtype=float)
    med_w = int(round(np.median(widths)))
    med_h = int(round(np.median(heights)))
    centers = sorted([r[0] + r[2] / 2 for r in row])
    diffs = np.diff(centers)
    diffs = diffs[(diffs > med_w * 0.75) & (diffs < med_w * 1.8)]
    if len(diffs) == 0:
        return row
    step = float(np.median(diffs))

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 45, 140)
    result = list(row)
    known_centers = centers[:]

    # Reconstruct only slots that have convincing frame-edge evidence.
    for direction in (-1, 1):
        base = min(known_centers) if direction < 0 else max(known_centers)
        for _ in range(3):
            expected = base + direction * step
            x = int(round(expected - med_w / 2))
            y = int(round(np.median([r[1] for r in row])))
            candidate = (x, y, med_w, med_h)
            if x < 0 or x + med_w > w or y < 0 or y + med_h > h:
                break
            if _edge_score(edges, candidate) < 28.0:
                break
            if any(abs((r[0] + r[2] / 2) - expected) < med_w * 0.35 for r in result):
                break
            result.append(candidate)
            known_centers.append(expected)
            base = expected
    return sorted(result, key=lambda r: r[0])


def _detect_rows(img: np.ndarray) -> list[list[tuple[int, int, int, int]]]:
    candidates = _candidate_rectangles(img)
    rows = _cluster_rows(candidates, img.shape[0])
    if rows:
        rows = [_complete_row(row, img) for row in rows]
        # Keep only the dominant card family. This naturally drops right-side panels.
        family_rows = []
        for row in rows:
            if len(row) >= 2:
                family_rows.append(row)
        return sorted(family_rows, key=lambda row: np.mean([r[1] for r in row]))

    # Last-resort adaptive grid for screenshots where contours are too weak.
    h, w = img.shape[:2]
    if w / float(h) > 1.35:
        return _fallback_wide_grid(img)
    return []


def _fallback_wide_grid(img: np.ndarray) -> list[list[tuple[int, int, int, int]]]:
    h, w = img.shape[:2]
    # Estimate a tall-card family from common PUBG inventory proportions.
    card_h = int(h * 0.49)
    card_w = int(card_h * 0.68)
    y = int(h * 0.35)
    x_start = int(w * 0.13)
    step = int(w * 0.168)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 45, 140)
    row = []
    for i in range(6):
        x = x_start + i * step
        rect = (x, y, card_w, card_h)
        if x + card_w > w * 0.9:
            break
        if _edge_score(edges, rect) >= 28.0:
            row.append(rect)
    return [row] if row else []


def detect_inventory_cards(image: Image.Image) -> list[dict[str, Any]]:
    pil = _image_from_any(image)
    img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    rows = _detect_rows(img)
    cards: list[dict[str, Any]] = []
    for r, row in enumerate(rows):
        for c, (x, y, w, h) in enumerate(row):
            cards.append({"row": r, "col": c, "box": [int(x), int(y), int(w), int(h)]})
    return cards


def extract_visual_area(card: Image.Image) -> Image.Image:
    w, h = card.size
    # Ignore small frame/quantity/name regions while keeping the actual artwork.
    return card.crop((int(w * 0.05), int(h * 0.06), int(w * 0.95), int(h * 0.78)))



# Counter templates are normalized 9x20 binary glyphs. They are based on the
# PUBG counter font visible in the supplied screenshots and cover quantities 2-9.
_COUNTER_TEMPLATES = {
    2: "011111100011111110111111111111001111111000111000000111000000111000001111000001111000001111000111110000111100001111000011110000011110000111100000111100000111111111111111111111111111",
    3: "011111100011111110111111111111001111111000111000000111000000111000001111000111110000111110000111110000111111000001111000000111000000111111000111111001111111111111111111110011111110",
    4: "000011100000011100000011100000111100000111100000111100000111100001101100001101100001101100011001100011001100110001100111111111111111111111111111011111110000001100000001100000001100",
    5: "111111111111111111111111111111000000111000000111000000111000000111111110111111111111111111110000111000000111000000111000000111000000111111000111111001111111111111011111110011111100",
    6: "001111100011111110011111111111101111111000111111000000111000000111111110111111111111111111111000111111000111111000111111000111111000111111000111111101111011111111011111110001111100",
    7: "111111111111111111111111111000001111000000111000001110000001110000011110000011100000011100000111000000111000000111000001110000001110000001110000001110000011110000011110000011110000",
    8: "011111110011111110111111111111001111111000111111000111111000111111001111011111110011111110111111111111111111111000111111000111111000111111000111111000111111111111011111110011111110",
    9: "011111100011111110111111110111101111111001111111000111111000111111000111111000111111000111111111111111111111011111111000000111000000111111001111111001111111111110011111100011111100",
}


def _template_image(value: str) -> np.ndarray:
    return np.array([int(ch) for ch in value], dtype=np.uint8).reshape(20, 9)


def _digit_score(mask: np.ndarray, digit: int) -> float:
    normalized = cv2.resize(mask.astype(np.uint8), (9, 20), interpolation=cv2.INTER_AREA)
    candidate = normalized >= 128
    template = _template_image(_COUNTER_TEMPLATES[digit]) > 0
    intersection = np.logical_and(candidate, template).sum()
    union = np.logical_or(candidate, template).sum()
    return float(intersection / union) if union else 0.0


def _detect_counter(card: Image.Image) -> tuple[Image.Image, int, float]:
    """
    Detect the small gray quantity badge in the card's upper-right corner.

    Returns:
        cleaned_card, quantity (1 when no badge is present), confidence.

    The badge itself is removed before hashing so ×2/×5/×7 do not create
    different identities for the same visual card.
    """
    arr = cv2.cvtColor(np.array(card.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)

    # The counter is always a small white glyph inside a gray badge at the
    # upper-right. Looking for the glyph is safer than thresholding the whole
    # gray rectangle because card artwork can also contain gray areas.
    rx1, ry1 = int(w * 0.62), 0
    rx2, ry2 = int(w * 0.995), int(h * 0.26)
    roi = gray[ry1:ry2, rx1:rx2]
    bright = cv2.inRange(roi, 190, 255)

    components, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    digit_candidates = []
    for label in range(1, components):
        x, y, cw, ch, area = stats[label]
        if area < 35 or area > 450:
            continue
        if ch < max(8, int(h * 0.045)) or ch > int(h * 0.14):
            continue
        if cw < 3 or cw > max(8, int(w * 0.09)):
            continue
        digit_candidates.append((label, x, y, cw, ch, area))

    if not digit_candidates:
        return card.convert("RGB"), 1, 0.0

    # The number is the rightmost suitable white component.
    digit_candidates.sort(key=lambda item: (item[1] + item[3], item[4]), reverse=True)
    label, dx, dy, dw, dh, _ = digit_candidates[0]

    # Reject unrelated artwork highlights that happen to be in the same ROI.
    absolute_x = rx1 + dx
    absolute_y = ry1 + dy
    if absolute_x < int(w * 0.70) or absolute_y > int(h * 0.22):
        return card.convert("RGB"), 1, 0.0

    mask = (labels == label).astype(np.uint8) * 255
    digit = mask[dy:dy + dh, dx:dx + dw]
    scores = sorted(
        ((_digit_score(digit, d), d) for d in _COUNTER_TEMPLATES),
        reverse=True,
    )
    confidence, quantity = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0.0

    # Require both a reasonable glyph match and separation from the next digit.
    if confidence < 0.42 or confidence - runner_up < 0.025:
        return card.convert("RGB"), 1, 0.0

    # The multiplication sign should be immediately to the left of the digit.
    left_region = bright[
        max(0, dy - int(dh * 0.25)):min(roi.shape[0], dy + dh + int(dh * 0.25)),
        max(0, dx - int(dw * 1.9)):dx,
    ]
    left_components, _, left_stats, _ = cv2.connectedComponentsWithStats(left_region, 8)
    has_multiplier = any(
        8 <= s[2] <= max(14, int(w * 0.08))
        and 5 <= s[3] <= max(16, int(h * 0.10))
        and 15 <= s[4] <= 180
        for s in left_stats[1:]
    )
    if not has_multiplier:
        return card.convert("RGB"), 1, 0.0

    # Remove the whole badge with a small inpaint margin. Keep the operation
    # local so card artwork outside the badge is untouched.
    x1 = max(0, absolute_x - int(dw * 1.65))
    y1 = max(0, absolute_y - int(dh * 0.38))
    x2 = min(w, absolute_x + dw + int(dw * 0.45))
    y2 = min(h, absolute_y + dh + int(dh * 0.45))
    inpaint_mask = np.zeros((h, w), dtype=np.uint8)
    inpaint_mask[y1:y2, x1:x2] = 255
    cleaned = cv2.inpaint(arr, inpaint_mask, 3, cv2.INPAINT_TELEA)
    cleaned_pil = Image.fromarray(cv2.cvtColor(cleaned, cv2.COLOR_BGR2RGB))

    return cleaned_pil, int(quantity), float(confidence)


def normalize_card(card: Image.Image) -> Image.Image:
    return card.convert("RGB").resize(NORMALIZED_SIZE, Image.Resampling.LANCZOS)


def calculate_card_hashes(card: Image.Image) -> dict[str, str]:
    full = normalize_card(card)
    visual = normalize_card(extract_visual_area(card))
    return {
        "full_phash": str(imagehash.phash(full)),
        "full_dhash": str(imagehash.dhash(full)),
        "visual_phash": str(imagehash.phash(visual)),
        "visual_dhash": str(imagehash.dhash(visual)),
    }


def save_preview(card: Image.Image, prefix: str = "card") -> str:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}_{int(time.time() * 1000)}_{np.random.randint(1000, 9999)}.jpg"
    path = TEMP_DIR / filename
    card.save(path, "JPEG", quality=92)
    return f"/temp_images/{filename}"


def scan_screenshot(image: Image.Image, save_previews: bool = True) -> dict[str, Any]:
    image = _image_from_any(image)
    detected = detect_inventory_cards(image)
    cards = []
    for item in detected:
        x, y, w, h = item["box"]
        crop = image.crop((x, y, x + w, y + h))
        clean_crop, quantity, quantity_confidence = _detect_counter(crop)
        raw_hashes = calculate_card_hashes(crop)
        hashes = calculate_card_hashes(clean_crop)
        entry = {
            "row": item["row"],
            "col": item["col"],
            "box": item["box"],
            "width": w,
            "height": h,
            "hashes": hashes,
            "raw_hashes": raw_hashes,
            "hash": hashes["full_dhash"],
            "quantity": quantity,
            "duplicates": max(0, quantity - 1),
            "quantity_confidence": round(quantity_confidence, 3),
        }
        if save_previews:
            entry["preview"] = save_preview(clean_crop)
        cards.append(entry)
    return {"cards": cards, "count": len(cards), "image_size": list(image.size)}
