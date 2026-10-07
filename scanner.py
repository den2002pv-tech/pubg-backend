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
    """Keep regular card runs and reject unrelated same-row UI panels.

    The detector must not assume a fixed number of cards or a fixed screen
    resolution. Real cards normally form a run with nearly identical
    dimensions and a repeatable center-to-center pitch. A right-side menu,
    popup, or other UI block usually breaks that geometry.
    """
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


def _single_card_candidate(img: np.ndarray) -> tuple[int, int, int, int] | None:
    """Find one large, centered card when no regular inventory row exists.

    This is intentionally a fallback: normal inventory detection keeps
    priority, while this branch accepts a single card that can occupy most
    of the screen vertically.
    """
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

        cx = x + cw / 2.0
        cy = y + ch / 2.0
        center_penalty = abs(cx - w / 2.0) / w + abs(cy - h / 2.0) / h
        edge = _edge_score(edges, (x, y, cw, ch))
        score = (edge / 100.0) + (area / (w * h)) - center_penalty * 0.55
        candidates.append((score, (x, y, cw, ch)))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    # Full-screen card viewer fallback.
    # Some PUBG screenshots show one enlarged card in the exact center while
    # the inventory behind it is dark/blurred. The decorative frame is not
    # necessarily one closed contour, so contour detection can return nothing.
    # Search a small family of centered card-shaped rectangles instead of
    # hashing the whole blurred screen.
    gray_f = gray.astype(np.float32)
    best: tuple[float, tuple[int, int, int, int]] | None = None

    for hf in np.linspace(0.70, 0.98, 8):
        for wf in np.linspace(0.24, 0.40, 9):
            cw = int(round(w * wf))
            ch = int(round(h * hf))
            if cw <= 0 or ch <= 0:
                continue

            ratio = cw / float(ch)
            if not 0.45 <= ratio <= 0.90:
                continue

            x = (w - cw) // 2
            y = (h - ch) // 2
            inner = gray_f[y:y + ch, x:x + cw]
            if inner.size == 0:
                continue

            side_left = gray_f[:, max(0, x - cw // 2):x]
            side_right = gray_f[:, min(w, x + cw):min(w, x + cw + cw // 2)]
            outside = np.concatenate([side_left, side_right], axis=1)
            if outside.size == 0:
                continue

            contrast = float(inner.mean() - outside.mean())

            bw = max(2, int(cw * 0.035))
            bh = max(2, int(ch * 0.035))
            strips = [
                inner[:, :bw],
                inner[:, -bw:],
                inner[:bh, :],
                inner[-bh:, :],
            ]
            edge = float(np.mean([
                np.mean(np.abs(np.diff(s, axis=1))) if s.shape[1] > 1 else 0.0
                for s in strips[:2]
            ] + [
                np.mean(np.abs(np.diff(s, axis=0))) if s.shape[0] > 1 else 0.0
                for s in strips[2:]
            ]))

            score = contrast + edge * 0.30
            if best is None or score > best[0]:
                best = (score, (x, y, cw, ch))

    if best is not None and best[0] >= 38.0:
        return best[1]

    return None



def _detect_rows(img: np.ndarray) -> list[list[tuple[int, int, int, int]]]:
    candidates = _candidate_rectangles(img)
    rows = _cluster_rows(candidates, img.shape[0])
    if rows:
        rows = [_complete_row(row, img) for row in rows]
        # Rows have already been reduced to regular card families.
        # Do not use a fixed left/right cutoff: users can have different
        # resolutions, aspect ratios, and different numbers of cards.
        return sorted(rows, key=lambda row: np.mean([r[1] for r in row]))

    # If there is no regular inventory row, try a centered single-card
    # viewer. This fallback deliberately does not invent a rectangle from
    # screen dimensions: it still requires a detected contour with card-like
    # geometry and visible border evidence.
    single = _single_card_candidate(img)
    if single is not None:
        return [[single]]

    # Never fabricate card rectangles from screen dimensions alone.
    # A generic/admin page can have strong edges in the same places as the
    # old fallback grid and would then be returned as fake inventory cards.
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
    # Ignore small frame/quantity/name regions while keeping the actual artwork.
    return card.crop((int(w * 0.05), int(h * 0.06), int(w * 0.95), int(h * 0.78)))


def extract_frame_area(card: Image.Image) -> Image.Image:
    """Build a compact image containing only the card's outer frame."""
    card = card.convert("RGB")
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
    frame = extract_frame_area(card)
    return {
        "full_phash": str(imagehash.phash(full)),
        "full_dhash": str(imagehash.dhash(full)),
        "visual_phash": str(imagehash.phash(visual)),
        "visual_dhash": str(imagehash.dhash(visual)),
        # pHash/dHash are grayscale and therefore can treat a blue and a
        # gold rarity frame as nearly identical. colorhash keeps the hue
        # information from the outer frame.
        "frame_colorhash": str(imagehash.colorhash(frame)),
    }


def encode_preview(card: Image.Image) -> str:
    """Encode a small preview in memory; never persist scan previews on Render."""
    image = card.convert("RGB")
    image.thumbnail((256, 384), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=82, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


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
            entry["preview"] = encode_preview(clean_crop)
        cards.append(entry)
    return {"cards": cards, "count": len(cards), "image_size": list(image.size)}
