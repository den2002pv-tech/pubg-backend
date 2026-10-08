from __future__ import annotations

from io import BytesIO
import base64
from typing import Any

import cv2
import imagehash
import numpy as np
from PIL import Image

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
        if cw < w * 0.055 or cw > w * 0.62:
            continue
        if ch < h * 0.18 or ch > h * 0.95:
            continue
        if not 0.45 <= ratio <= 0.90:
            continue
        if area < w * h * 0.012:
            continue
        bw = max(2, int(cw * 0.035))
        bh = max(2, int(ch * 0.035))
        strips = (
            edges[y:y + ch, x:x + bw],
            edges[y:y + ch, max(x, x + cw - bw):x + cw],
            edges[y:y + bh, x:x + cw],
            edges[max(y, y + ch - bh):y + ch, x:x + cw],
        )
        side_scores = [float(s.mean()) / 255.0 if s.size else 0.0 for s in strips]
        if sum(v >= 0.035 for v in side_scores) < 3:
            continue
        out.append((x, y, cw, ch))
    return out


def _card_geometry_similar(
    rect: tuple[int, int, int, int],
    med_w: float,
    med_h: float,
    tolerance: float = 0.18,
) -> bool:
    _, _, w, h = rect
    return (
        abs(w - med_w) <= med_w * tolerance
        and abs(h - med_h) <= med_h * tolerance
    )


def _regular_card_runs(
    row: list[tuple[int, int, int, int]],
) -> list[list[tuple[int, int, int, int]]]:
    if len(row) < 2:
        return []

    widths = np.array([r[2] for r in row], dtype=float)
    heights = np.array([r[3] for r in row], dtype=float)
    med_w = float(np.median(widths))
    med_h = float(np.median(heights))
    ordered = sorted(row, key=lambda r: r[0])

    runs: list[list[tuple[int, int, int, int]]] = []
    current = [ordered[0]]

    for rect in ordered[1:]:
        prev = current[-1]
        prev_cx = prev[0] + prev[2] / 2
        cx = rect[0] + rect[2] / 2
        center_gap = cx - prev_cx

        compatible = (
            _card_geometry_similar(rect, med_w, med_h)
            and 0.82 * med_w <= center_gap <= 1.70 * med_w
        )
        if compatible:
            current.append(rect)
        else:
            if len(current) >= 2:
                runs.append(current)
            current = [rect]

    if len(current) >= 2:
        runs.append(current)

    return runs


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

    result: list[list[tuple[int, int, int, int]]] = []
    for row in rows:
        for run in _regular_card_runs(row):
            result.append(sorted(run, key=lambda r: r[0]))
    return result


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

    if len(result) >= 4:
        return sorted(result, key=lambda r: r[0])

    for direction in (-1, 1):
        base = min(known_centers) if direction < 0 else max(known_centers)
        for _ in range(3):
            expected = base + direction * step
            x = int(round(expected - med_w / 2))
            y = int(round(np.median([r[1] for r in row])))
            candidate = (x, y, med_w, med_h)
            if x < 0 or x + med_w > w or y < 0 or y + med_h > h:
                break
            if len(result) >= 4 or x + med_w > w * 0.82:
                break
            if _edge_score(edges, candidate) < 28.0:
                break
            if any(abs((r[0] + r[2] / 2) - expected) < med_w * 0.35 for r in result):
                break
            result.append(candidate)
            known_centers.append(expected)
            base = expected
    return sorted(result, key=lambda r: r[0])


def _single_card_candidate(img: np.ndarray) -> tuple[int, int, int, int] | None:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 45, 140)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []

    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        ratio = cw / float(ch or 1)
        area = cw * ch

        if cw < w * 0.20 or cw > w * 0.90:
            continue
        if ch < h * 0.30 or ch > h * 0.92:
            continue
        if not 0.45 <= ratio <= 0.90:
            continue
        if area < w * h * 0.08:
            continue

        bw = max(2, int(cw * 0.035))
        bh = max(2, int(ch * 0.035))
        strips = (
            edges[y:y + ch, x:x + bw],
            edges[y:y + ch, max(x, x + cw - bw):x + cw],
            edges[y:y + bh, x:x + cw],
            edges[max(y, y + ch - bh):y + ch, x:x + cw],
        )
        side_scores = [float(s.mean()) / 255.0 if s.size else 0.0 for s in strips]
        if sum(v >= 0.035 for v in side_scores) < 3:
            continue

        cx = x + cw / 2.0
        cy = y + ch / 2.0
        center_penalty = abs(cx - w / 2.0) / w + abs(cy - h / 2.0) / h
        edge = _edge_score(edges, (x, y, cw, ch))
        score = (edge / 100.0) + (area / (w * h)) - center_penalty * 0.55
        candidates.append((score, (x, y, cw, ch)))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    gray_f = gray.astype(np.float32)
    col_band = gray_f[int(h * 0.04):int(h * 0.96), :]
    col_mean = col_band.mean(axis=0)
    outer_cols = np.concatenate([
        col_mean[:max(1, int(w * 0.15))],
        col_mean[min(w - 1, int(w * 0.85)):],
    ])
    col_delta = col_mean - float(outer_cols.mean())
    active_x = np.where(col_delta > 20.0)[0]

    row_band = gray_f[:, int(w * 0.35):int(w * 0.65)]
    row_mean = row_band.mean(axis=1)
    outer_rows = np.concatenate([
        row_mean[:max(1, int(h * 0.12))],
        row_mean[min(h - 1, int(h * 0.88)):],
    ])
    row_delta = row_mean - float(outer_rows.mean())
    active_y = np.where(row_delta > 20.0)[0]

    if active_x.size and active_y.size:
        x1, x2 = int(active_x.min()), int(active_x.max()) + 1
        y1, y2 = int(active_y.min()), int(active_y.max()) + 1
        cw, ch = x2 - x1, y2 - y1
        ratio = cw / float(ch or 1)
        center_x = (x1 + x2) / 2.0

        if (
            0.45 <= ratio <= 0.90
            and cw >= w * 0.20
            and ch >= h * 0.55
            and abs(center_x - w / 2.0) <= w * 0.10
        ):
            bw = max(2, int(cw * 0.035))
            bh = max(2, int(ch * 0.035))
            strips = (
                edges[y1:y2, x1:x1 + bw],
                edges[y1:y2, max(x1, x2 - bw):x2],
                edges[y1:y1 + bh, x1:x2],
                edges[max(y1, y2 - bh):y2, x1:x2],
            )
            side_scores = [float(s.mean()) / 255.0 if s.size else 0.0 for s in strips]
            if sum(v >= 0.035 for v in side_scores) < 3:
                return None
            pad_x = max(2, int(cw * 0.012))
            pad_y = max(2, int(ch * 0.012))
            x1 = max(0, x1 - pad_x)
            y1 = max(0, y1 - pad_y)
            x2 = min(w, x2 + pad_x)
            y2 = min(h, y2 + pad_y)
            return (x1, y1, x2 - x1, y2 - y1)

    return None


def _detect_rows(img: np.ndarray) -> list[list[tuple[int, int, int, int]]]:
    candidates = _candidate_rectangles(img)
    rows = _cluster_rows(candidates, img.shape[0])
    if rows:
        rows = [_complete_row(row, img) for row in rows]
        return sorted(rows, key=lambda row: np.mean([r[1] for r in row]))

    single = _single_card_candidate(img)
    if single is not None:
        return [[single]]
    return []


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
    return card.crop((int(w * 0.05), int(h * 0.06), int(w * 0.95), int(h * 0.78)))


def extract_frame_area(card: Image.Image) -> Image.Image:
    card = card.convert("RGB")

    arr = np.asarray(card)
    luminance = arr.mean(axis=2)
    active = luminance > 8.0

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
    if mask is None or mask.size == 0 or mask.shape[0] < 3 or mask.shape[1] < 3:
        return 0.0
    normalized = cv2.resize(mask.astype(np.uint8), (9, 20), interpolation=cv2.INTER_AREA)
    candidate = normalized >= 128
    template = _template_image(_COUNTER_TEMPLATES[digit]) > 0
    intersection = np.logical_and(candidate, template).sum()
    union = np.logical_or(candidate, template).sum()
    return float(intersection / union) if union else 0.0


def _multiply_score(mask: np.ndarray) -> float:
    if mask is None or mask.size == 0 or mask.shape[0] < 3 or mask.shape[1] < 3:
        return 0.0

    normalized = cv2.resize(mask.astype(np.uint8), (15, 15), interpolation=cv2.INTER_AREA)
    candidate = normalized >= 128
    template = np.zeros((15, 15), dtype=np.uint8)
    cv2.line(template, (3, 3), (11, 11), 255, 2)
    cv2.line(template, (11, 3), (3, 11), 255, 2)
    target = template > 0

    intersection = np.logical_and(candidate, target).sum()
    union = np.logical_or(candidate, target).sum()
    iou = float(intersection / union) if union else 0.0

    fill = float(candidate.mean())
    return iou if 0.035 <= fill <= 0.45 else 0.0


def _detect_counter(card: Image.Image) -> tuple[Image.Image, int, float, dict[str, Any]]:
    card = card.convert("RGB")
    arr = np.asarray(card)
    h, w = arr.shape[:2]

    if h < 20 or w < 20:
        return card, 1, 0.0, {"roi": None, "candidates": 0, "selected": None, "reason": "card_too_small"}

    # The counter badge is a UI element in the upper-right corner of the card.
    # Work only inside this small ROI so card artwork is not interpreted as a digit.
    x1 = int(w * 0.45)
    y1 = 0
    x2 = max(x1 + 1, int(w * 0.995))
    y2 = max(y1 + 1, int(h * 0.28))

    roi = arr[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
    bright = cv2.inRange(gray, 190, 255)

    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[dict[str, Any]] = []

    roi_h, roi_w = gray.shape[:2]
    for contour in contours:
        cx, cy, cw, ch = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if ch < max(4, int(roi_h * 0.10)) or ch > int(roi_h * 0.90):
            continue
        if cw < 2 or cw > max(8, int(roi_w * 0.30)):
            continue
        if area < 3.0:
            continue

        mask = np.zeros((ch, cw), dtype=np.uint8)
        shifted = contour - np.array([[[cx, cy]]], dtype=np.int32)
        cv2.drawContours(mask, [shifted], -1, 255, thickness=-1)

        scores = {digit: _digit_score(mask, digit) for digit in range(2, 10)}
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_digit, best_score = ordered[0]
        runner_up = ordered[1][1]

        if best_score < 0.42 or best_score - runner_up < 0.025:
            continue

        candidates.append({
            "x": x1 + cx,
            "y": y1 + cy,
            "w": cw,
            "h": ch,
            "digit": best_digit,
            "score": best_score,
        })

    if not candidates:
        return card, 1, 0.0, {
            "roi": [x1, y1, x2, y2],
            "candidates": 0,
            "selected": None,
            "reason": "no_confident_digit",
        }

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[0]
    digit_x = int(selected["x"])
    digit_y = int(selected["y"])
    digit_w = int(selected["w"])
    digit_h = int(selected["h"])

    # Validate the multiplication sign separately. It must look like an actual
    # X, not merely be another bright rectangular contour.
    sign_candidates: list[tuple[float, int, int, int, int]] = []
    search_left = max(x1, digit_x - int(w * 0.12))
    sign_region = bright[
        max(0, digit_y - int(digit_h * 0.35)):min(roi_h, digit_y + digit_h + int(digit_h * 0.35)),
        search_left - x1:digit_x - x1,
    ]

    if sign_region.size:
        sign_contours, _ = cv2.findContours(sign_region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        sy0 = max(0, digit_y - int(digit_h * 0.35))
        for contour in sign_contours:
            sx, sy, sw, sh = cv2.boundingRect(contour)
            if sw < 3 or sh < 3:
                continue
            if sw > digit_h * 1.25 or sh > digit_h * 1.25:
                continue
            area = cv2.contourArea(contour)
            if area < 2.0:
                continue

            mask = np.zeros((sh, sw), dtype=np.uint8)
            shifted = contour - np.array([[[sx, sy]]], dtype=np.int32)
            cv2.drawContours(mask, [shifted], -1, 255, thickness=-1)
            score = _multiply_score(mask)
            sign_candidates.append((score, search_left + sx, sy0 + sy, sw, sh))

    if not sign_candidates:
        return card, 1, 0.0, {
            "roi": [x1, y1, x2, y2],
            "candidates": len(candidates),
            "selected": {"digit": selected["digit"], "score": round(float(selected["score"]), 3)},
            "reason": "multiply_sign_not_found",
        }

    sign_candidates.sort(key=lambda item: item[0], reverse=True)
    sign_score, sign_x, sign_y, sign_w, sign_h = sign_candidates[0]
    if sign_score < 0.30:
        return card, 1, 0.0, {
            "roi": [x1, y1, x2, y2],
            "candidates": len(candidates),
            "selected": {"digit": selected["digit"], "score": round(float(selected["score"]), 3)},
            "reason": "multiply_sign_low_confidence",
            "multiply_score": round(float(sign_score), 3),
        }

    # Remove only the detected UI badge from the hash input. Geometry stays
    # unchanged and the user-facing preview continues to use the original crop.
    # Expand to the whole small UI badge, not only the white glyphs.
    # This removes the gray square behind ×N from the hash input as well.
    left = max(0, min(sign_x, digit_x) - int(w * 0.025))
    right = min(w, max(sign_x + sign_w, digit_x + digit_w) + int(w * 0.045))
    top = max(0, min(sign_y, digit_y) - int(h * 0.035))
    bottom = min(h, max(sign_y + sign_h, digit_y + digit_h) + int(h * 0.045))

    clean = arr.copy()
    patch = clean[max(0, top - 1):min(h, bottom + 1), max(0, left - 1):min(w, right + 1)]
    if patch.size:
        clean[max(0, top):bottom, max(0, left):right] = np.median(patch, axis=(0, 1)).astype(np.uint8)

    clean_card = Image.fromarray(clean, mode="RGB")
    return clean_card, int(selected["digit"]), float(min(selected["score"], sign_score)), {
        "roi": [x1, y1, x2, y2],
        "candidates": len(candidates),
        "selected": {
            "digit": selected["digit"],
            "digit_score": round(float(selected["score"]), 3),
            "multiply_score": round(float(sign_score), 3),
            "box": [left, top, right - left, bottom - top],
        },
        "reason": "ok",
    }

def normalize_card(card: Image.Image) -> Image.Image:
    return card.convert("RGB").resize(NORMALIZED_SIZE, Image.Resampling.LANCZOS)


def is_gray_locked_card(card: Image.Image) -> bool:
    arr = np.asarray(card.convert("RGB"))
    if arr.size == 0:
        return False
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    h, w = hsv.shape[:2]
    border = np.concatenate([
        hsv[:max(1, int(h * 0.12)), :, 1].ravel(),
        hsv[max(0, int(h * 0.88)):, :, 1].ravel(),
        hsv[:, :max(1, int(w * 0.10)), 1].ravel(),
        hsv[:, max(0, int(w * 0.90)):, 1].ravel(),
    ])
    if border.size == 0:
        return False
    return float(np.mean(border)) < 20.0 and float(np.percentile(border, 90)) < 48.0


def calculate_card_hashes(card: Image.Image) -> dict[str, str]:
    full = normalize_card(card)
    visual = normalize_card(extract_visual_area(card))
    frame = extract_frame_area(card)
    return {
        "full_phash": str(imagehash.phash(full)),
        "full_dhash": str(imagehash.dhash(full)),
        "visual_phash": str(imagehash.phash(visual)),
        "visual_dhash": str(imagehash.dhash(visual)),
        "frame_colorhash": str(imagehash.colorhash(frame)),
    }


def encode_preview(card: Image.Image) -> str:
    image = card.convert("RGB")
    image.thumbnail((256, 384), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=82, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def scan_screenshot(image: Image.Image, save_previews: bool = True) -> dict[str, Any]:
    image = _image_from_any(image)
    detected = detect_inventory_cards(image)
    cards = []
    for item in detected:
        x, y, w, h = item["box"]
        crop = image.crop((x, y, x + w, y + h))
        if is_gray_locked_card(crop):
            continue
        clean_crop, quantity, quantity_confidence, counter_debug = _detect_counter(crop)
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
            "counter_debug": counter_debug,
        }
        if save_previews:
            # Keep the original card in the preview; UI cleanup is hash-only.
            entry["preview"] = encode_preview(crop)
        cards.append(entry)
    return _json_safe({"cards": cards, "count": len(cards), "image_size": list(image.size)})
