from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import cv2
import imagehash
import numpy as np
from PIL import Image

TEMP_DIR = Path("temp_images")
NORMALIZED_SIZE = (224, 320)
MATCH_THRESHOLD = 18.0


def load_image(source: str | Path | bytes) -> np.ndarray:
    if isinstance(source, (str, Path)):
        data = np.fromfile(str(source), dtype=np.uint8)
    else:
        data = np.frombuffer(source, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Не удалось открыть изображение")
    return img


def _candidate_rectangles(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find tall card-like rectangles. The important part is shape, not exact pixels."""
    h_img, w_img = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    edges = cv2.Canny(gray, 40, 150)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=2)
    closed = cv2.dilate(closed, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    result: list[tuple[int, int, int, int]] = []

    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < 0.055 * w_img or w > 0.48 * w_img:
            continue
        if h < 0.20 * h_img or h > 0.80 * h_img:
            continue

        aspect = w / max(h, 1)
        if not 0.42 <= aspect <= 0.92:
            continue

        # Ignore tiny/noisy shapes.
        if w * h < 0.010 * w_img * h_img:
            continue

        result.append((x, y, w, h))

    # Deduplicate nested/near-identical rectangles.
    result.sort(key=lambda r: r[2] * r[3], reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for r in result:
        x, y, w, h = r
        too_close = False
        for q in kept:
            qx, qy, qw, qh = q
            ix1, iy1 = max(x, qx), max(y, qy)
            ix2, iy2 = min(x + w, qx + qw), min(y + h, qy + qh)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            union = w * h + qw * qh - inter
            if union and inter / union > 0.82:
                too_close = True
                break
        if not too_close:
            kept.append(r)

    return kept


def _row_groups(rects: list[tuple[int, int, int, int]]) -> list[list[tuple[int, int, int, int]]]:
    if not rects:
        return []

    # Cards in one row have close vertical centers and similar heights.
    heights = np.array([r[3] for r in rects], dtype=float)
    med_h = float(np.median(heights))
    y_tol = max(24.0, med_h * 0.18)

    rows: list[list[tuple[int, int, int, int]]] = []
    for r in sorted(rects, key=lambda z: z[1] + z[3] / 2):
        cy = r[1] + r[3] / 2
        placed = False
        for row in rows:
            row_cy = np.mean([q[1] + q[3] / 2 for q in row])
            if abs(cy - row_cy) <= y_tol:
                row.append(r)
                placed = True
                break
        if not placed:
            rows.append([r])

    return [sorted(row, key=lambda z: z[0]) for row in rows]


def _best_chain(row: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Pick the repeated card family and reject side panels."""
    if len(row) <= 2:
        return row

    widths = np.array([r[2] for r in row], dtype=float)
    heights = np.array([r[3] for r in row], dtype=float)
    med_w, med_h = float(np.median(widths)), float(np.median(heights))

    # Keep cards whose size is close to the dominant family.
    family = [
        r for r in row
        if abs(r[2] - med_w) <= 0.18 * med_w
        and abs(r[3] - med_h) <= 0.18 * med_h
    ]
    if len(family) <= 2:
        family = row

    family = sorted(family, key=lambda z: z[0])
    if len(family) <= 2:
        return family

    centers = np.array([r[0] + r[2] / 2 for r in family], dtype=float)
    gaps = np.diff(centers)
    if len(gaps) == 0:
        return family

    # The normal card-to-card step is the lower/median repeated gap.
    step = float(np.median(gaps))
    if step <= 0:
        return family

    # Longest contiguous chain whose gap is close to the dominant step.
    best: list[tuple[int, int, int, int]] = []
    cur = [family[0]]
    for i, gap in enumerate(gaps):
        if 0.72 * step <= gap <= 1.35 * step:
            cur.append(family[i + 1])
        else:
            if len(cur) > len(best):
                best = cur
            cur = [family[i + 1]]
    if len(cur) > len(best):
        best = cur

    return best if len(best) >= 2 else family


def _complete_lattice(row: list[tuple[int, int, int, int]], image_shape: tuple[int, int, int], blockers: list[tuple[int, int, int, int]] | None = None) -> list[tuple[int, int, int, int]]:
    """Recover a card whose border was not detected, using repeated spacing."""
    if len(row) < 2:
        return row

    h_img, w_img = image_shape[:2]
    row = sorted(row, key=lambda r: r[0])

    widths = np.array([r[2] for r in row], dtype=float)
    heights = np.array([r[3] for r in row], dtype=float)
    w = int(round(float(np.median(widths))))
    h = int(round(float(np.median(heights))))
    centers = np.array([r[0] + r[2] / 2 for r in row], dtype=float)
    gaps = np.diff(centers)
    step = float(np.median(gaps))
    if step < 0.8 * w:
        step = float(w + np.median(np.diff([r[0] for r in row])) - w) if len(row) > 1 else float(w)

    # If there is a large internal gap, fill integer multiples of the step.
    completed = list(row)
    expected_centers = [float(centers[0])]
    for i in range(1, len(centers)):
        delta = centers[i] - centers[i - 1]
        n = max(1, int(round(delta / step)))
        for k in range(1, n + 1):
            expected_centers.append(float(centers[i - 1] + delta * k / n))

    # Extend one slot left/right only when the inferred slot remains well inside
    # the screen. This is what recovers a missing edge card without hardcoding 1536x691.
    left = float(centers[0])
    right = float(centers[-1])
    blocker_centers = [r[0] + r[2] / 2 for r in (blockers or [])]

    for _ in range(3):
        target = left - step
        if any(0 < bc - left < 1.45 * step for bc in blocker_centers):
            break
        if target - w / 2 >= 0.02 * w_img and target > 0.65 * w:
            expected_centers.insert(0, target)
            left = target
        else:
            break

    for _ in range(3):
        target = right + step
        if any(0 < bc - right < 1.45 * step for bc in blocker_centers):
            break
        if target + w / 2 <= 0.96 * w_img:
            expected_centers.append(target)
            right = target
        else:
            break

    # Keep only plausible slots. Do not create slots that correspond to a
    # rejected candidate (for example a right-side panel that has the same
    # card-like shape).
    blocker_centers = [r[0] + r[2] / 2 for r in (blockers or [])]
    out: list[tuple[int, int, int, int]] = []
    for cx in expected_centers:
        if any(abs(bc - cx) < 0.55 * step for bc in blocker_centers):
            continue
        cy = float(np.median([r[1] + r[3] / 2 for r in row]))
        x = int(round(cx - w / 2))
        y = int(round(cy - h / 2))
        if x < 0 or y < 0 or x + w > w_img or y + h > h_img:
            continue
        if any(abs((r[0] + r[2] / 2) - cx) < 0.35 * step for r in out):
            continue
        out.append((x, y, w, h))

    return sorted(out, key=lambda r: r[0])


def detect_inventory_cards(img: np.ndarray) -> list[dict[str, Any]]:
    rects = _candidate_rectangles(img)
    rows = _row_groups(rects)

    # Discard isolated small/odd rows; a real inventory row normally has >=2 cards.
    usable_rows = [r for r in rows if len(r) >= 2]
    cards: list[dict[str, Any]] = []

    for row in usable_rows:
        chain = _best_chain(row)
        chain = _complete_lattice(chain, img.shape, [r for r in rects if r not in chain])
        if len(chain) < 2:
            continue

        for col, (x, y, w, h) in enumerate(chain):
            cards.append({
                "row": len([r for r in usable_rows[:usable_rows.index(row)] for _ in r]),
                "col": col,
                "box": [int(x), int(y), int(w), int(h)],
                "width": int(w),
                "height": int(h),
            })

    # Re-number rows cleanly.
    if cards:
        by_y = {}
        for c in cards:
            by_y.setdefault(c["box"][1], []).append(c)
        ordered_y = sorted(by_y)
        for ri, yy in enumerate(ordered_y):
            for ci, c in enumerate(sorted(by_y[yy], key=lambda z: z["box"][0])):
                c["row"], c["col"] = ri, ci

    return cards


def normalize_card(card_bgr: np.ndarray) -> Image.Image:
    rgb = cv2.cvtColor(card_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb).resize(NORMALIZED_SIZE, Image.Resampling.LANCZOS)


def extract_visual_area(card_bgr: np.ndarray) -> np.ndarray:
    h, w = card_bgr.shape[:2]
    # Ignore a small frame and the bottom quantity/title strip.
    x1, x2 = int(w * 0.05), int(w * 0.95)
    y1, y2 = int(h * 0.05), int(h * 0.72)
    return card_bgr[y1:y2, x1:x2]


def calculate_card_hashes(card_bgr: np.ndarray) -> dict[str, str]:
    full = normalize_card(card_bgr)
    visual = normalize_card(extract_visual_area(card_bgr))
    return {
        "full_phash": str(imagehash.phash(full)),
        "full_dhash": str(imagehash.dhash(full)),
        "visual_phash": str(imagehash.phash(visual)),
        "visual_dhash": str(imagehash.dhash(visual)),
    }


def compare_card_hashes(a: dict[str, str] | str, b: dict[str, str] | str) -> float:
    if isinstance(a, str):
        a = {"full_dhash": a}
    if isinstance(b, str):
        b = {"full_dhash": b}

    scores = []
    for key in ("visual_phash", "visual_dhash", "full_phash", "full_dhash"):
        if key in a and key in b:
            try:
                ha = imagehash.hex_to_hash(str(a[key]))
                hb = imagehash.hex_to_hash(str(b[key]))
                scores.append(ha - hb)
            except Exception:
                pass

    return float(np.mean(scores)) if scores else 999.0


def find_best_match(hashes: dict[str, str], database: dict[str, Any], threshold: float = MATCH_THRESHOLD):
    best_id = None
    best_score = 999.0
    for card_id, card in database.items():
        other = card.get("hashes") if isinstance(card, dict) else card
        if not other or str(other).lower() == "undefined":
            continue
        score = compare_card_hashes(hashes, other)
        if score < best_score:
            best_id, best_score = card_id, score
    return (best_id, best_score) if best_id is not None and best_score <= threshold else (None, best_score)


def save_preview(img: np.ndarray, box: tuple[int, int, int, int], name: str) -> str:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    x, y, w, h = box
    crop = img[y:y+h, x:x+w]
    path = TEMP_DIR / name
    cv2.imwrite(str(path), crop)
    return f"/temp_images/{path.name}"


def save_debug_image(img: np.ndarray, cards: list[dict[str, Any]], name: str = "debug_scan.jpg") -> str:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    out = img.copy()
    for card in cards:
        x, y, w, h = card["box"]
        cv2.rectangle(out, (x, y), (x+w, y+h), (0, 220, 0), 3)
        cv2.putText(out, f'{card["row"]}:{card["col"]}', (x+5, y+24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 0), 2)
    path = TEMP_DIR / name
    cv2.imwrite(str(path), out)
    return f"/temp_images/{path.name}"


def scan_screenshot(source: str | Path | bytes, *, save_previews: bool = True, save_debug: bool = True) -> dict[str, Any]:
    img = load_image(source)
    cards = detect_inventory_cards(img)

    for i, card in enumerate(cards):
        x, y, w, h = card["box"]
        crop = img[y:y+h, x:x+w]
        card["hashes"] = calculate_card_hashes(crop)
        # Legacy compatibility: old admin.html expected a single "hash".
        card["hash"] = card["hashes"]["full_dhash"]
        if save_previews:
            card["preview"] = save_preview(
                img, (x, y, w, h),
                f"card_r{card['row']}_c{card['col']}_{i}.jpg"
            )

    result = {"cards": cards}
    if save_debug:
        result["debug_image"] = save_debug_image(img, cards)
    return result


def match_scan_results(scan: dict[str, Any], database: dict[str, Any]) -> dict[str, Any]:
    matches = []
    for card in scan.get("cards", []):
        card_id, score = find_best_match(card["hashes"], database)
        matches.append({
            "row": card["row"],
            "col": card["col"],
            "box": card["box"],
            "card_id": card_id,
            "score": None if score >= 999 else round(score, 3),
            "preview": card.get("preview"),
        })
    return {"matches": matches}
