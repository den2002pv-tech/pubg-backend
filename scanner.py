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


_COUNTER_TEMPLATES: dict[int, str] = {
    0: (
        "00111110001111111011111111111100011111100011111100011111100011111100"
        "01111110001111110001111110001111110001111110001111110001111110001111"
        "11000111111000111111111111011111110001111100"
    ),
    1: (
        "00111110001111110011111110011011110000011110000011110000011110000011"
        "11000001111000001111000001111000001111000001111000001111000001111000"
        "00111100000111100000111100000111100000111100"
    ),
    2: "011111100011111110111111111111001111111000111000000111000000111000001111000001111000001111000111110000111100001111000011110000011110000111100000111100000111111111111111111111111111",
    3: "011111100011111110111111111111001111111000111000000111000000111000001111000111110000111110000111110000111111000001111000000111000000111111000111111001111111111111111111110011111110",
    4: "000011100000011100000011100000111100000111100000111100000111100001101100001101100001101100011001100011001100110001100111111111111111111111111111011111110000001100000001100000001100",
    5: "111111111111111111111111111111000000111000000111000000111000000111111110111111111111111111110000111000000111000000111000000111000000111111000111111001111111111111011111110011111100",
    6: "001111100011111110011111111111101111111000111111000000111000000111111110111111111111111111111000111111000111111000111111000111111000111111000111111101111011111111011111110001111100",
    7: "111111111111111111111111111000001111000000111000001110000001110000011110000011100000011100000111000000111000000111000001110000001110000001110000001110000011110000011110000011110000",
    8: "011111110011111110111111111111001111111000111111000111111000111111001111011111110011111110111111111111111111111000111111000111111000111111000111111000111111111111011111110011111110",
    9: "011111100011111110111111110111101111111001111111000111111000111111000111111000111111000111111111111111111111011111111000000111000000111111001111111001111111111110011111100011111100",
}

_COUNTER_VARIANTS: dict[int, list[str]] = {
    1: [
        (
            "00111110000111110000111110000111110000111110000111110000111110000111"
            "11000011111000011111000011111000011111000011111000011111000011111000"
            "01111100001111100001111100001111100001111100"
        ),
        (
            "00111110001111110011111110011011110000011111000011111000011111000011"
            "11100001111100001111100001111100001111100001111100001111100001111100"
            "00111110000111110000111110000111110000111110"
        ),
        "1" * 180,
    ]
}


def _template_image(value: str) -> np.ndarray:
    if len(value) != 180:
        raise ValueError(f"Counter template must contain 180 pixels, got {len(value)}")
    return np.array([int(ch) for ch in value], dtype=np.uint8).reshape(20, 9)


def _digit_score(mask: np.ndarray, digit: int) -> float:
    if mask is None or mask.size == 0 or mask.shape[0] < 3 or mask.shape[1] < 3:
        return 0.0
    normalized = cv2.resize(mask.astype(np.uint8), (9, 20), interpolation=cv2.INTER_AREA)
    candidate = normalized >= 128
    templates = [_COUNTER_TEMPLATES[digit]] + _COUNTER_VARIANTS.get(digit, [])
    best = 0.0
    for tmpl_str in templates:
        target = _template_image(tmpl_str) > 0
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
    return best


def _multiply_score(mask: np.ndarray) -> float:
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


def _detect_counter(card: Image.Image) -> tuple[Image.Image, int, float, dict[str, Any]]:
    card = card.convert("RGB")
    arr = np.asarray(card)
    h, w = arr.shape[:2]
    if h < 20 or w < 20:
        return card, 1, 0.0, {"roi": None, "candidates": 0, "selected": None, "reason": "card_too_small"}

    x1 = int(w * 0.60)
    y1 = max(0, int(h * 0.01))
    x2 = min(w, int(w * 0.985))
    y2 = min(h, int(h * 0.20))
    roi = arr[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
    roi_h, roi_w = gray.shape[:2]
    x_candidates: list[dict[str, Any]] = []
    digit_candidates: list[dict[str, Any]] = []
    threshold_runs: list[dict[str, Any]] = []

    def _process_contours(contour_list: list[np.ndarray]) -> None:
        for contour in contour_list:
            cx, cy, cw, ch = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if ch < max(4, int(roi_h * 0.12)) or ch > int(roi_h * 0.85):
                continue
            if cw < 2 or cw > max(8, int(roi_w * 0.40)) or area < 3.0:
                continue
            mask = np.zeros((ch, cw), dtype=np.uint8)
            shifted = contour - np.array([[[cx, cy]]], dtype=np.int32)
            cv2.drawContours(mask, [shifted], -1, 255, thickness=-1)

            if cw >= 3 and ch >= 3 and 0.55 <= cw / float(ch) <= 1.45:
                x_score = _multiply_score(mask)
                if x_score >= 0.30:
                    x_candidates.append({"x": x1 + cx, "y": y1 + cy, "w": cw, "h": ch, "score": x_score})

            if ch >= max(5, int(roi_h * 0.16)) and cw / float(ch) <= 0.88:
                scores = {d: _digit_score(mask, d) for d in range(10)}
                ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
                best_digit, best_score = ordered[0]
                runner_up = ordered[1][1]
                if best_score >= 0.45 and (best_score - runner_up >= 0.03 or best_score >= 0.65):
                    digit_candidates.append({
                        "x": x1 + cx, "y": y1 + cy, "w": cw, "h": ch,
                        "digit": best_digit, "score": best_score,
                    })

    for thresh in (180, 160):
        bright = cv2.inRange(gray, thresh, 255)
        join_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        glyph_mask = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, join_kernel, iterations=1)
        contours, _ = cv2.findContours(glyph_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        x_before, digits_before = len(x_candidates), len(digit_candidates)
        _process_contours(contours)
        threshold_runs.append({
            "threshold": thresh,
            "contours": len(contours),
            "bright_pixel_ratio": round(float(np.count_nonzero(bright)) / float(bright.size or 1), 4),
            "x_candidates_added": len(x_candidates) - x_before,
            "digit_candidates_added": len(digit_candidates) - digits_before,
        })
        if x_candidates and digit_candidates:
            break

    candidate_diagnostics = {
        "threshold_runs": threshold_runs,
        "x_candidates_found": len(x_candidates),
        "digit_candidates_found": len(digit_candidates),
        "x_candidates": [
            {k: round(float(v), 3) if k == "score" else int(v) for k, v in item.items()}
            for item in sorted(x_candidates, key=lambda item: item["score"], reverse=True)[:5]
        ],
        "digit_candidates": [
            {k: round(float(v), 3) if k == "score" else int(v) for k, v in item.items()}
            for item in sorted(digit_candidates, key=lambda item: item["score"], reverse=True)[:10]
        ],
    }

    if not x_candidates or not digit_candidates:
        return card, 1, 0.0, {
            "roi": [x1, y1, x2, y2], "candidates": 0, "selected": None,
            "reason": "no_glyph_candidates", **candidate_diagnostics,
        }

    badge_candidates: list[dict[str, Any]] = []
    for xc in x_candidates:
        for d1 in digit_candidates:
            if d1["x"] <= xc["x"]:
                continue
            gap = d1["x"] - (xc["x"] + xc["w"])
            if gap < 0 or gap > max(5, int(d1["h"] * 0.45)):
                continue
            if not (0.35 * d1["h"] <= xc["h"] <= 0.90 * d1["h"]):
                continue
            if abs((xc["y"] + xc["h"] / 2.0) - (d1["y"] + d1["h"] / 2.0)) > max(5, int(d1["h"] * 0.40)):
                continue

            combinations: list[tuple[list[dict[str, Any]], int]] = []
            if d1["digit"] >= 2:
                combinations.append(([d1], d1["digit"]))
            if d1["digit"] >= 1:
                matching = []
                for d2 in digit_candidates:
                    if d2 is d1 or d2["x"] <= d1["x"]:
                        continue
                    d2_gap = d2["x"] - (d1["x"] + d1["w"])
                    if d2_gap < 0 or d2_gap > max(4, int(d1["h"] * 0.40)):
                        continue
                    if not (0.80 * d1["h"] <= d2["h"] <= 1.25 * d1["h"]):
                        continue
                    if abs(d1["y"] - d2["y"]) > max(3, int(d1["h"] * 0.25)):
                        continue
                    if abs((d1["y"] + d1["h"]) - (d2["y"] + d2["h"])) > max(3, int(d1["h"] * 0.25)):
                        continue
                    matching.append(d2)
                if matching:
                    matching.sort(key=lambda d: d["x"])
                    d2 = matching[0]
                    combinations.append(([d1, d2], d1["digit"] * 10 + d2["digit"]))

            for digits, quantity in combinations:
                if quantity < 2:
                    continue
                g_left = xc["x"]
                g_right = digits[-1]["x"] + digits[-1]["w"]
                g_top = min(xc["y"], min(d["y"] for d in digits))
                g_bottom = max(xc["y"] + xc["h"], max(d["y"] + d["h"] for d in digits))
                g_w, g_h = g_right - g_left, g_bottom - g_top
                if g_left < int(w * 0.62) or g_right > int(w * 0.99):
                    continue
                if g_top < int(h * 0.015) or g_bottom > int(h * 0.20):
                    continue
                if not (int(h * 0.035) <= g_h <= int(h * 0.12)):
                    continue
                aspect = g_w / float(g_h)
                if len(digits) == 1 and not (0.85 <= aspect <= 2.2):
                    continue
                if len(digits) == 2 and not (1.10 <= aspect <= 3.2):
                    continue

                pad_x, pad_y = max(2, int(g_h * 0.25)), max(2, int(g_h * 0.20))
                b_left, b_right = max(0, g_left - pad_x), min(w, g_right + pad_x)
                b_top, b_bottom = max(0, g_top - pad_y), min(h, g_bottom + pad_y)
                patch = arr[b_top:b_bottom, b_left:b_right]
                if patch.size == 0:
                    continue
                patch_gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
                bg_pixels = patch_gray[patch_gray < 165]
                if bg_pixels.size < 8:
                    continue
                bg_median = float(np.median(bg_pixels))
                glyph_pixels = patch_gray[patch_gray >= 165]
                glyph_mean = float(np.mean(glyph_pixels)) if glyph_pixels.size else 255.0
                contrast = glyph_mean - bg_median
                if contrast < 55.0 or bg_median > 155.0:
                    continue

                digit_scores = [d["score"] for d in digits]
                confidence = min(xc["score"], min(digit_scores))
                avg_confidence = (xc["score"] + sum(digit_scores)) / (1.0 + len(digits))
                badge_candidates.append({
                    "quantity": quantity, "confidence": confidence, "avg_confidence": avg_confidence,
                    "xc": xc, "digits": digits,
                    "box": [b_left, b_top, b_right - b_left, b_bottom - b_top],
                    "x_score": xc["score"], "digit_scores": digit_scores,
                    "contrast": round(contrast, 1), "bg_median": round(bg_median, 1),
                })

    if not badge_candidates:
        return card, 1, 0.0, {
            "roi": [x1, y1, x2, y2], "candidates": 0, "selected": None,
            "reason": "no_valid_badge", **candidate_diagnostics,
            "rejection_stage": "badge_geometry_or_contrast",
        }

    badge_candidates.sort(
        key=lambda b: (
            len(b["digits"]) if b["confidence"] >= 0.40 else 0,
            b["confidence"], b["avg_confidence"],
        ),
        reverse=True,
    )
    best = badge_candidates[0]
    b_left, b_top, b_w, b_h = best["box"]
    b_right, b_bottom = b_left + b_w, b_top + b_h
    clean = arr.copy()
    fill_left = max(0, b_left - int(w * 0.015))
    fill_right = min(w, b_right + int(w * 0.015))
    fill_top = max(0, b_top - int(h * 0.015))
    fill_bottom = min(h, b_bottom + int(h * 0.015))
    border_patch = clean[
        max(0, fill_top - 2):min(h, fill_bottom + 2),
        max(0, fill_left - 2):min(w, fill_right + 2),
    ]
    if border_patch.size:
        clean[fill_top:fill_bottom, fill_left:fill_right] = np.median(border_patch, axis=(0, 1)).astype(np.uint8)

    clean_card = Image.fromarray(clean, mode="RGB")
    return clean_card, int(best["quantity"]), float(min(1.0, best["confidence"])), {
        "roi": [x1, y1, x2, y2],
        "candidates": len(badge_candidates),
        "selected": {
            "quantity": best["quantity"],
            "digit": best["digits"][0]["digit"] if len(best["digits"]) == 1 else None,
            "digits": [d["digit"] for d in best["digits"]],
            "digit_score": round(float(best["digits"][0]["score"]), 3),
            "digit_scores": [round(float(s), 3) for s in best["digit_scores"]],
            "multiply_score": round(float(best["x_score"]), 3),
            "box": best["box"], "contrast": best["contrast"], "bg_median": best["bg_median"],
        },
        "reason": "ok",
        **candidate_diagnostics,
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
