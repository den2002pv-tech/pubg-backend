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
        hashes = calculate_card_hashes(crop)
        entry = {
            "row": item["row"],
            "col": item["col"],
            "box": item["box"],
            "width": w,
            "height": h,
            "hashes": hashes,
            "hash": hashes["full_dhash"],
        }
        if save_previews:
            entry["preview"] = save_preview(crop)
        cards.append(entry)
    return {"cards": cards, "count": len(cards), "image_size": list(image.size)}
