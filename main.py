from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from pydantic import BaseModel

from scanner import calculate_card_hashes, detect_inventory_cards, scan_screenshot
from database import add_zero_cards, clear_user_cards, get_desired_cards, get_or_create_user, get_user_cards, init_db, set_desired_cards, set_user_card_quantity, sync_cards

app = FastAPI(title="PUBG Card Scanner")


@app.on_event("startup")
def startup_database() -> None:
    init_db()
    _migrate_frame_color_hashes()
    sync_cards(load_db())

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pubg-app-phi.vercel.app",
    ],
    # Vercel preview deployments get a different origin. Keep the rule
    # restricted to this application's Vercel project instead of allowing
    # every origin, because admin requests use bearer credentials.
    allow_origin_regex=r"^https://pubg-app-[a-z0-9-]+\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path("static")
CARDS_DIR = STATIC_DIR / "cards"
CARDS_DIR.mkdir(parents=True, exist_ok=True)

# Admin workflow previews are temporary. Render's free filesystem is ephemeral.
TEMP_DIR = Path("temp_images")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Small in-memory ring buffer: bounded for Render's 0.1 CPU/free instance.
# Logs contain detector diagnostics only, never credentials or source images.
SCAN_DIAGNOSTIC_LOGS: deque[dict[str, Any]] = deque(maxlen=100)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/temp-images", StaticFiles(directory=str(TEMP_DIR)), name="temp-images")

DB_FILE = Path("card_hashes.json")
SESSION_MAX_AGE = 12 * 60 * 60
ADMIN_TOKEN_PREFIX = "pubg_admin_v1"

HASH_REGEN_PROGRESS: dict[str, Any] = {
    "running": False,
    "current": 0,
    "total": 0,
    "card_id": "",
    "status": "idle",
    "updated": 0,
    "error": "",
}




def load_db() -> dict[str, Any]:
    if not DB_FILE.exists():
        return {}
    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_db(data: dict[str, Any]) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_FILE)


def clean_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, val in value.items():
        if val is None:
            continue
        s = str(val).strip()
        if not s or s.lower() in {"undefined", "null", "none"}:
            continue
        result[str(key)] = s
    return result


def valid_hash(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return bool(s) and s not in {"undefined", "null", "none"}


def _session_secret() -> str:
    secret = os.getenv("ADMIN_SESSION_SECRET", "")
    if not secret:
        secret = os.getenv("ADMIN_PASSWORD", "")
    if not secret:
        # The application will reject admin requests when ADMIN_PASSWORD
        # is missing, so this value is only a defensive fallback.
        return "invalid-admin-secret"
    return secret


def _make_session() -> str:
    """
    Create a signed bearer token.

    Format:
        pubg_admin_v1.<timestamp>.<random_nonce>.<hmac>

    The token is self-contained, so Render does not need a session database.
    """
    ts_s = str(int(time.time()))
    nonce = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    payload = f"{ADMIN_TOKEN_PREFIX}.{ts_s}.{nonce}"
    sig = hmac.new(
        _session_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def _valid_session(value: str | None) -> bool:
    if not value:
        return False

    parts = value.split(".")
    if len(parts) != 4:
        return False

    prefix, ts_s, nonce, sig = parts
    if prefix != ADMIN_TOKEN_PREFIX or not nonce or not sig:
        return False

    try:
        ts = int(ts_s)
    except ValueError:
        return False

    now = time.time()
    if now - ts > SESSION_MAX_AGE or ts > now + 60:
        return False

    payload = f"{prefix}.{ts_s}.{nonce}"
    expected = hmac.new(
        _session_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(sig, expected)


def _validate_telegram_init_data(init_data: str) -> dict[str, Any]:
    """Validate Telegram Mini App initData and return the Telegram user."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN не задан на Render")

    from urllib.parse import parse_qsl

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Отсутствует Telegram hash")

    auth_date = pairs.get("auth_date", "")
    try:
        if time.time() - int(auth_date) > 86400:
            raise HTTPException(status_code=401, detail="Telegram initData устарел")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Некорректный Telegram auth_date") from exc

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Недействительные данные Telegram")

    try:
        user = json.loads(pairs.get("user", "{}"))
        telegram_id = int(user["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя Telegram") from exc

    return {"id": telegram_id, "user": user}


def _authorization_token(authorization: str | None) -> str | None:
    if not authorization:
        return None

    scheme, sep, token = authorization.partition(" ")
    if not sep or scheme.lower() != "bearer":
        return None

    token = token.strip()
    return token or None


def require_admin(authorization: str | None) -> None:
    if not os.getenv("ADMIN_PASSWORD"):
        raise HTTPException(
            status_code=503,
            detail="На Render не задан ADMIN_PASSWORD",
        )

    token = _authorization_token(authorization)
    if not _valid_session(token):
        raise HTTPException(status_code=401, detail="Требуется вход в админку")


class LoginSchema(BaseModel):
    password: str


class TelegramAuthSchema(BaseModel):
    init_data: str


class CounterTestCaseSchema(BaseModel):
    preview: str
    expected_quantity: int
    detected_quantity: int = 1
    quantity_confidence: float = 0.0
    card_id: str = ""
    card_name: str = ""
    row: int | None = None
    col: int | None = None
    box: list[int] | None = None
    image_size: list[int] | None = None
    counter_debug: dict[str, Any] = {}


class CardSaveSchema(BaseModel):
    id: str = ""
    name: str
    rarity: int = 1
    preview: str = ""
    hash: str | None = None
    hashes: dict[str, str] | None = None


def _hamming(a: str, b: str) -> int | None:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return None


ICON_MATCH_THRESHOLD = float(os.getenv("ICON_MATCH_THRESHOLD", "10"))
CARD_MATCH_STRONG_DISTANCE = int(os.getenv("CARD_MATCH_STRONG_DISTANCE", "7"))
CARD_MATCH_ACCEPTABLE_DISTANCE = int(os.getenv("CARD_MATCH_ACCEPTABLE_DISTANCE", "14"))
CARD_MATCH_MIN_STRONG_HASHES = int(os.getenv("CARD_MATCH_MIN_STRONG_HASHES", "2"))
CARD_MATCH_MIN_ACCEPTABLE_HASHES = int(os.getenv("CARD_MATCH_MIN_ACCEPTABLE_HASHES", "3"))
CARD_MATCH_COLOR_MAX_RATIO = float(os.getenv("CARD_MATCH_COLOR_MAX_RATIO", "0.35"))
# A fallback for animated cards whose hashes are consistently shifted: if the
# best candidate is clearly ahead of the runner-up, 3 acceptable hashes plus
# a compatible frame color can be enough even without 2 "strong" hashes.
CARD_MATCH_CONFIDENT_MARGIN = float(os.getenv("CARD_MATCH_CONFIDENT_MARGIN", "5.0"))


def _migrate_frame_color_hashes() -> None:
    """Add frame color hashes to older cards using their saved icons."""
    db = load_db()
    changed = False

    for card_id, info in db.items():
        if not isinstance(info, dict):
            continue

        hashes = clean_hashes(info.get("hashes"))
        if valid_hash(hashes.get("frame_colorhash")):
            continue

        icon_path = CARDS_DIR / f"{card_id}.webp"
        if not icon_path.is_file():
            continue

        try:
            with Image.open(icon_path) as icon:
                new_hashes = calculate_card_hashes(icon.convert("RGB"))
        except Exception:
            continue

        frame_hash = new_hashes.get("frame_colorhash")
        if not valid_hash(frame_hash):
            continue

        hashes["frame_colorhash"] = frame_hash
        info["hashes"] = hashes
        info["hash"] = hashes.get("full_dhash") or info.get("hash", "")
        changed = True

    if changed:
        save_db(db)


def _match_card(card_hashes: dict[str, str], db: dict[str, Any]) -> tuple[str | None, float]:
    """
    Match by hash voting, with a conservative confidence-margin fallback.

    Normal matches still require the configured strong/acceptable vote. For
    animated cards, where every hash can shift by a few bits, a candidate may
    pass without two strong hashes only when:
      - it has at least 3 acceptable grayscale hashes;
      - it passes the existing outlier safety ceiling;
      - its frame color is compatible;
      - it is clearly ahead of the runner-up by CARD_MATCH_CONFIDENT_MARGIN.

    This avoids globally relaxing the strong threshold.
    """
    candidates: list[dict[str, Any]] = []
    standard_keys = (
        "full_phash",
        "full_dhash",
        "visual_phash",
        "visual_dhash",
    )

    for card_id, info in db.items():
        stored = clean_hashes(info.get("hashes"))
        distances: list[int] = []

        for key in standard_keys:
            value = card_hashes.get(key)
            other = stored.get(key)
            if not value or not other:
                continue
            d = _hamming(value, other)
            if d is not None:
                distances.append(d)

        if not distances:
            legacy = info.get("hash")
            value = card_hashes.get("full_dhash", "")
            d = _hamming(value, str(legacy)) if legacy else None
            if d is not None:
                distances.append(d)

        if len(distances) < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            continue

        distances.sort()
        strong_count = sum(d <= CARD_MATCH_STRONG_DISTANCE for d in distances)
        acceptable_count = sum(d <= CARD_MATCH_ACCEPTABLE_DISTANCE for d in distances)

        if acceptable_count < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            continue

        if distances[-1] > CARD_MATCH_ACCEPTABLE_DISTANCE + 6:
            continue

        scan_color = card_hashes.get("frame_colorhash")
        stored_color = stored.get("frame_colorhash")

        if scan_color and not stored_color:
            continue

        if scan_color and stored_color:
            color_distance = _hamming(scan_color, stored_color)
            if color_distance is None:
                continue
            color_bits = max(1, len(scan_color) * 4)
            color_ratio = color_distance / color_bits
            if color_ratio > 0.55 and strong_count < 3:
                continue
            color_penalty = color_ratio * 8.0
        else:
            color_ratio = 0.0
            color_penalty = 0.0

        score = (distances[0] + distances[1]) / 2 + color_penalty
        candidates.append({
            "id": card_id,
            "score": score,
            "strong": strong_count,
            "acceptable": acceptable_count,
            "color_ratio": color_ratio,
        })

    if not candidates:
        return None, float("inf")

    candidates.sort(key=lambda item: item["score"])
    best = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None

    # Preferred path: the original strong-vote rule.
    if best["strong"] >= CARD_MATCH_MIN_STRONG_HASHES:
        return best["id"], best["score"]

    # Conservative fallback for animated/dynamic cards. Do not simply raise
    # the strong threshold globally: that would weaken discrimination for
    # visually similar static cards. Instead require a clear score margin.
    margin = (
        runner_up["score"] - best["score"]
        if runner_up is not None
        else float("inf")
    )
    if (
        best["acceptable"] >= CARD_MATCH_MIN_ACCEPTABLE_HASHES
        and best["color_ratio"] <= CARD_MATCH_COLOR_MAX_RATIO
        and margin > CARD_MATCH_CONFIDENT_MARGIN
    ):
        return best["id"], best["score"]

    return None, float("inf")

def _match_card_diagnostics(card_hashes: dict[str, str], db: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """Return ranked match diagnostics for admin troubleshooting.

    This does not change matching behavior. It exposes the individual hash
    distances and gate results so a bad recognition can be traced to either
    the crop/detection or the matcher without dumping full card records.
    """
    standard_keys = (
        "full_phash",
        "full_dhash",
        "visual_phash",
        "visual_dhash",
    )
    rows: list[dict[str, Any]] = []

    for card_id, info in db.items():
        stored = clean_hashes(info.get("hashes"))
        pairs: list[tuple[str, int]] = []
        for key in standard_keys:
            value = card_hashes.get(key)
            other = stored.get(key)
            if value and other:
                d = _hamming(value, other)
                if d is not None:
                    pairs.append((key, d))

        distances = sorted(d for _, d in pairs)
        if not distances:
            legacy = info.get("hash")
            value = card_hashes.get("full_dhash", "")
            d = _hamming(value, str(legacy)) if legacy else None
            if d is not None:
                distances = [d]

        strong_count = sum(d <= CARD_MATCH_STRONG_DISTANCE for d in distances)
        acceptable_count = sum(d <= CARD_MATCH_ACCEPTABLE_DISTANCE for d in distances)
        max_distance = max(distances) if distances else None

        scan_color = card_hashes.get("frame_colorhash")
        stored_color = stored.get("frame_colorhash")
        color_ratio = None
        color_status = "not_checked"
        if scan_color and not stored_color:
            color_status = "stored_hash_missing"
        elif scan_color and stored_color:
            color_distance = _hamming(scan_color, stored_color)
            if color_distance is not None:
                color_ratio = round(color_distance / max(1, len(scan_color) * 4), 3)
                color_status = "ok" if color_ratio <= 0.55 else "high"

        reasons: list[str] = []
        if len(distances) < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            reasons.append("less_than_3_hashes")
        if strong_count < CARD_MATCH_MIN_STRONG_HASHES:
            reasons.append("less_than_2_strong")
        if acceptable_count < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            reasons.append("less_than_3_acceptable")
        if max_distance is not None and max_distance > CARD_MATCH_ACCEPTABLE_DISTANCE + 6:
            reasons.append("hash_outlier_too_large")
        if scan_color and not stored_color:
            reasons.append("stored_color_hash_missing")
        if color_ratio is not None and color_ratio > 0.55 and strong_count < 3:
            reasons.append("color_mismatch")

        if len(distances) >= 2:
            score = (distances[0] + distances[1]) / 2
            if color_ratio is not None:
                score += color_ratio * 8.0
        else:
            score = float("inf")

        rows.append({
            "id": card_id,
            "name": info.get("name", card_id),
            "distances": {key: d for key, d in pairs},
            "sorted_distances": distances,
            "strong": strong_count,
            "acceptable": acceptable_count,
            "max": max_distance,
            "color_ratio": color_ratio,
            "color_status": color_status,
            "score": None if not math.isfinite(score) else round(score, 2),
            "accepted": not reasons,
            "reasons": reasons,
        })

    rows.sort(key=lambda item: item["score"] if item["score"] is not None else float("inf"))

    # Mirror the confidence-margin fallback in _match_card so the admin
    # diagnostics explain why an animated card was accepted or rejected.
    if rows:
        best = rows[0]
        runner = rows[1] if len(rows) > 1 else None
        margin = (
            runner["score"] - best["score"]
            if runner and runner["score"] is not None and best["score"] is not None
            else float("inf")
        )
        best["runner_up_id"] = runner["id"] if runner else None
        best["runner_up_score"] = runner["score"] if runner else None
        best["score_margin"] = None if not math.isfinite(margin) else round(margin, 2)

        fallback_ok = (
            best["acceptable"] >= CARD_MATCH_MIN_ACCEPTABLE_HASHES
            and (best["color_ratio"] is not None and best["color_ratio"] <= CARD_MATCH_COLOR_MAX_RATIO)
            and margin > CARD_MATCH_CONFIDENT_MARGIN
        )
        best["confidence_fallback"] = fallback_ok
        if fallback_ok and best["strong"] < CARD_MATCH_MIN_STRONG_HASHES:
            best["accepted"] = True
            best["reasons"] = ["accepted_by_confident_margin"]

    return rows[:max(1, limit)]


def _next_card_id(db: dict[str, Any]) -> str:
    numbers = []

    for card_id in db:
        if not isinstance(card_id, str):
            continue

        if not card_id.startswith("card_"):
            continue

        suffix = card_id[5:]

        if suffix.isdigit():
            numbers.append(int(suffix))

    next_number = max(numbers, default=0) + 1

    while f"card_{next_number}" in db:
        next_number += 1

    return f"card_{next_number}"


def _enrich_admin_scan(
    cards: list[dict[str, Any]],
    db: dict[str, Any],
) -> list[dict[str, Any]]:
    result = []

    for card in cards:
        item = dict(card)
        diagnostics = _match_card_diagnostics(card.get("hashes", {}), db)
        item["debug"] = {
            "detector": {
                "box": card.get("box"),
                "width": card.get("width"),
                "height": card.get("height"),
                "ratio": round(card["width"] / max(1, card["height"]), 3) if card.get("height") else None,
                "row": card.get("row"),
                "col": card.get("col"),
            },
            "counter": {
                "quantity": card.get("quantity", 1),
                "confidence": card.get("quantity_confidence", 0),
                "details": card.get("counter_debug", {}),
            },
            "top_matches": diagnostics,
        }

        best_id, best_distance = _match_card(
            card.get("hashes", {}),
            db,
        )
        raw_id, raw_distance = _match_card(
            card.get("raw_hashes", {}),
            db,
        )
        if raw_id is not None and raw_distance < best_distance:
            best_id, best_distance = raw_id, raw_distance

        matched = (
            best_id is not None
            and best_distance <= float(
                os.getenv("CARD_MATCH_THRESHOLD", "18")
            )
        )

        if matched:
            info = db[best_id]

            item.update({
                "matched": True,
                "id": best_id,
                "name": info.get("name", best_id),
                "rarity": info.get("rarity", 1),
                "distance": round(best_distance, 2),
                "icon": info.get("icon", ""),
            })
        else:
            item.update({
                "matched": False,
                "id": None,
                "name": "",
                "rarity": None,
                "distance": (
                    round(best_distance, 2)
                    if best_id is not None
                    else None
                ),
                "icon": "",
            })

        result.append(item)

    next_number = max(
        (
            int(card_id[5:])
            for card_id in db
            if (
                isinstance(card_id, str)
                and card_id.startswith("card_")
                and card_id[5:].isdigit()
            )
        ),
        default=0,
    ) + 1

    for item in result:
        if not item["matched"]:
            while f"card_{next_number}" in db:
                next_number += 1

            item["suggested_id"] = f"card_{next_number}"
            next_number += 1

    return result


async def _read_image(file: UploadFile) -> Image.Image:
    try:
        data = await file.read()
        return Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Не удалось прочитать изображение") from exc


def _prepare_icon(image: Image.Image) -> Image.Image:
    """
    UI icon: keep the card aspect ratio and fit it into a 256x384 canvas.
    WebP is much smaller than the original JPEG/PNG.
    """
    image = image.convert("RGB")
    target_w, target_h = 256, 384
    scale = min(target_w / image.width, target_h / image.height)
    new_size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    resized = image.resize(new_size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGB", (target_w, target_h), "black")
    x = (target_w - resized.width) // 2
    y = (target_h - resized.height) // 2
    canvas.paste(resized, (x, y))
    return canvas


def _save_counter_fixture(image: Image.Image, box: Any, index: int) -> str:
    """Save the original-resolution card crop used by the detector for regression tests."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return ""
    try:
        x, y, w, h = (int(value) for value in box)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > image.width or y + h > image.height:
            return ""
        crop = image.crop((x, y, x + w, y + h)).convert("RGB")
        filename = f"counter-fixture-{int(time.time() * 1000)}-{index}.jpg"
        crop.save(TEMP_DIR / filename, format="JPEG", quality=92, optimize=True)
        return f"/temp-images/{filename}"
    except Exception:
        return ""

def _save_admin_preview(data_url: str, index: int) -> str:
    """Persist one temporary admin card preview on Render."""
    if not data_url.startswith("data:image/") or "," not in data_url:
        return data_url
    try:
        encoded = data_url.split(",", 1)[1]
        payload = base64.b64decode(encoded)
        filename = f"scan-{int(time.time() * 1000)}-{index}.jpg"
        path = TEMP_DIR / filename
        path.write_bytes(payload)
        return f"/temp-images/{filename}"
    except Exception:
        return data_url


def _extract_single_card(image: Image.Image) -> Image.Image:
    """
    For the icon tab the user normally uploads one whole card image.
    If CV can find exactly one card rectangle, use that crop. Otherwise
    treat the uploaded image itself as the card.
    """
    try:
        detected = detect_inventory_cards(image)
        if len(detected) == 1:
            x, y, w, h = detected[0]["box"]
            return image.crop((x, y, x + w, y + h))
    except Exception:
        pass
    return image


@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.get("/admin")
async def serve_admin():
    return FileResponse("admin.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/cards")
async def get_cards_endpoint(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return load_db()


@app.get("/admin/status")
async def admin_status(authorization: str | None = Header(default=None)):
    logged_in = bool(os.getenv("ADMIN_PASSWORD")) and _valid_session(
        _authorization_token(authorization)
    )
    return {
        "logged_in": logged_in,
        "cards": len(load_db()),
        "icons": sum(1 for x in load_db().values() if x.get("icon")),
        "temporary_files": sum(1 for p in TEMP_DIR.iterdir() if p.is_file()),
    }


@app.post("/admin/login")
async def admin_login(data: LoginSchema):
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD не задан на Render")

    if not hmac.compare_digest(data.password, password):
        raise HTTPException(status_code=401, detail="Неверный пароль")

    return {
        "status": "ok",
        "token": _make_session(),
        "expires_in": SESSION_MAX_AGE,
    }


@app.post("/admin/logout")
async def admin_logout():
    # Bearer tokens are kept only by the browser. Logout removes the token
    # on the client; the signed token naturally expires after SESSION_MAX_AGE.
    return {"status": "ok"}


@app.post("/admin/scan-preview")
async def scan_preview(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    started = time.perf_counter()
    log_entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "scan_preview",
        "app_commit": os.getenv("RENDER_GIT_COMMIT", "unknown"),
    }
    try:
        image = await _read_image(file)
        log_entry["image_size"] = [image.width, image.height]
        result = scan_screenshot(image, save_previews=True)
        for index, card in enumerate(result.get("cards", [])):
            card["counter_fixture"] = _save_counter_fixture(image, card.get("box"), index)
            if card.get("preview"):
                card["preview"] = _save_admin_preview(card["preview"], index)
        db = load_db()
        result["cards"] = _enrich_admin_scan(result.get("cards", []), db)
        log_entry.update({
            "status": "ok",
            "detected_count": result.get("count", len(result.get("cards", []))),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "cards": [
                {
                    "row": card.get("row"),
                    "col": card.get("col"),
                    "box": card.get("box"),
                    "matched": card.get("matched"),
                    "card_id": card.get("id"),
                    "name": card.get("name"),
                    "quantity": card.get("quantity"),
                    "quantity_confidence": card.get("quantity_confidence"),
                    "counter": card.get("debug", {}).get("counter", card.get("counter_debug", {})),
                }
                for card in result.get("cards", [])
            ],
        })
        SCAN_DIAGNOSTIC_LOGS.append(log_entry)
        return result
    except Exception as exc:
        log_entry.update({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        SCAN_DIAGNOSTIC_LOGS.append(log_entry)
        raise


@app.get("/admin/scan-logs")
async def download_scan_logs(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_commit": os.getenv("RENDER_GIT_COMMIT", "unknown"),
        "retention": "Most recent 100 scan-preview requests in this running process; cleared on restart/redeploy.",
        "count": len(SCAN_DIAGNOSTIC_LOGS),
        "logs": list(SCAN_DIAGNOSTIC_LOGS),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"pubg-scan-logs-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



COUNTER_TEST_CASES_PATH = "tests/fixtures/counter/cases.json"
COUNTER_TEST_IMAGES_PATH = "tests/fixtures/counter/images"


def _counter_test_branch() -> str:
    return os.getenv("COUNTER_TEST_BRANCH", "feature/card-counter").strip() or "feature/card-counter"


def _github_read_text(path: str, branch: str) -> str | None:
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    if not token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN не задан на Render")
    url = f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(branch, safe='')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pubg-card-admin",
    }
    try:
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return base64.b64decode(payload.get("content", "")).decode("utf-8")
    except HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GitHub API: {exc.code} {detail[:300]}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API недоступен: {exc}") from exc


def _github_commit_files(files: dict[str, bytes], message: str, branch: str) -> dict[str, str]:
    """Commit only the supplied files to the explicitly selected working branch."""
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    if not token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN не задан на Render")
    api = f"https://api.github.com/repos/{repo}"
    ref = _github_request("GET", f"{api}/git/ref/heads/{quote(branch, safe='')}", token)
    parent_sha = ref["object"]["sha"]
    parent = _github_request("GET", f"{api}/git/commits/{parent_sha}", token)
    entries = []
    for path, content in files.items():
        blob = _github_request("POST", f"{api}/git/blobs", token, {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = _github_request("POST", f"{api}/git/trees", token, {
        "base_tree": parent["tree"]["sha"],
        "tree": entries,
    })
    commit = _github_request("POST", f"{api}/git/commits", token, {
        "message": message,
        "tree": tree["sha"],
        "parents": [parent_sha],
    })
    _github_request("PATCH", f"{api}/git/refs/heads/{quote(branch, safe='')}", token, {
        "sha": commit["sha"],
        "force": False,
    })
    return {"sha": commit["sha"], "url": commit.get("html_url", ""), "branch": branch}


@app.get("/admin/counter-tests")
async def get_counter_tests(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    try:
        cases = json.loads(raw) if raw else {"version": 1, "cases": []}
        if not isinstance(cases, dict) or not isinstance(cases.get("cases"), list):
            cases = {"version": 1, "cases": []}
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Файл тестовых случаев в GitHub содержит некорректный JSON")
    return {
        "branch": branch,
        "count": len(cases["cases"]),
        "recent": [
            {
                "id": item.get("id"),
                "timestamp": item.get("timestamp"),
                "card_name": item.get("card_name"),
                "expected_quantity": item.get("expected_quantity"),
                "detected_quantity": item.get("detected_quantity"),
                "correct": item.get("correct"),
                "image_path": item.get("image_path"),
                "commit_url": item.get("commit_url"),
            }
            for item in reversed(cases["cases"][-10:])
        ],
    }


@app.post("/admin/counter-tests")
async def save_counter_test(
    data: CounterTestCaseSchema,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    if data.expected_quantity < 1 or data.expected_quantity > 999:
        raise HTTPException(status_code=400, detail="Реальное количество должно быть от 1 до 999")
    # Accept only previews generated by this admin scanner; never accept arbitrary paths.
    filename = Path(data.preview.split("?", 1)[0]).name
    if not filename.startswith("counter-fixture-") or Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=400, detail="Повторите сканирование, чтобы получить оригинальный кроп карты")
    image_path = TEMP_DIR / filename
    if not image_path.is_file():
        raise HTTPException(status_code=410, detail="Временное изображение уже удалено. Повторите сканирование.")
    image_bytes = image_path.read_bytes()
    if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Размер тестового изображения должен быть меньше 8 МБ")

    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    try:
        payload = json.loads(raw) if raw else {"version": 1, "cases": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Не удалось прочитать cases.json из рабочей ветки") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        payload = {"version": 1, "cases": []}

    image_sha = hashlib.sha256(image_bytes).hexdigest()
    timestamp = datetime.now(timezone.utc)
    case_id = f"{timestamp.strftime('%Y%m%dT%H%M%S%f')}-{image_sha[:10]}"
    relative_image = f"{COUNTER_TEST_IMAGES_PATH}/{case_id}.jpg"
    counter_debug = data.counter_debug if isinstance(data.counter_debug, dict) else {}
    try:
        with Image.open(BytesIO(image_bytes)) as fixture_image:
            detector_input_size = list(fixture_image.size)
            detector_input_format = fixture_image.format
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Временное изображение теста повреждено") from exc
    case = {
        "id": case_id,
        "timestamp": timestamp.isoformat(),
        "app_commit": os.getenv("RENDER_GIT_COMMIT", "unknown"),
        "card_id": data.card_id,
        "card_name": data.card_name,
        "row": data.row,
        "col": data.col,
        "card_box_in_screenshot": data.box,
        "source_screenshot_size": data.image_size,
        "detector_input_size": detector_input_size,
        "detector_input_format": detector_input_format,
        "image_path": relative_image,
        "image_sha256": image_sha,
        "expected_quantity": data.expected_quantity,
        "detected_quantity": data.detected_quantity,
        "correct": data.expected_quantity == data.detected_quantity,
        "quantity_confidence": data.quantity_confidence,
        "counter_debug": counter_debug,
    }
    payload.setdefault("version", 1)
    payload["cases"].append(case)
    payload["cases"] = payload["cases"][-500:]
    message = f"Add counter test case {case_id} (expected {data.expected_quantity}, detected {data.detected_quantity})"
    commit = _github_commit_files({
        relative_image: image_bytes,
        COUNTER_TEST_CASES_PATH: json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    }, message, branch)
    case["commit_url"] = commit["url"]
    # Add the resulting commit URL to the persisted record as well. A small follow-up
    # update is avoided: the URL is returned to the admin and visible in recent tests.
    log_entry = {
        "timestamp": timestamp.isoformat(),
        "event": "counter_test_labeled",
        "app_commit": os.getenv("RENDER_GIT_COMMIT", "unknown"),
        "git_commit": commit["sha"],
        "branch": branch,
        "case_id": case_id,
        "expected_quantity": data.expected_quantity,
        "detected_quantity": data.detected_quantity,
        "correct": case["correct"],
        "image_path": relative_image,
        "counter_debug": counter_debug,
    }
    SCAN_DIAGNOSTIC_LOGS.append(log_entry)
    return {
        "status": "saved",
        "case": case,
        "git_commit": commit["sha"],
        "commit_url": commit["url"],
        "branch": branch,
        "count": len(payload["cases"]),
    }


@app.get("/admin/temp-files")
async def list_temp_files(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    files = []
    for path in sorted(TEMP_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file():
            files.append({
                "name": path.name,
                "url": f"/temp-images/{path.name}",
                "size": path.stat().st_size,
            })
    return files


@app.delete("/admin/temp-files")
async def delete_temp_files(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    deleted = 0
    for path in TEMP_DIR.glob("*"):
        if path.is_file():
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    return {"status": "ok", "deleted": deleted}


@app.get("/me/cards")
async def get_my_cards(init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    auth = _validate_telegram_init_data(init_data)
    get_or_create_user(auth["id"])
    user = auth["user"]
    return {
        "registered": True,
        "telegram_id": auth["id"],
        "user": {
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
        },
        "cards": get_user_cards(auth["id"]),
        "desired_cards": get_desired_cards(auth["id"]),
    }


@app.put("/me/desired-cards")
async def update_desired_cards(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    card_ids = data.get("card_ids", [])
    if not isinstance(card_ids, list):
        raise HTTPException(status_code=400, detail="card_ids должен быть массивом")
    if len(card_ids) > 200:
        raise HTTPException(status_code=400, detail="Слишком много желаемых карт")
    get_or_create_user(auth["id"])
    desired = set_desired_cards(auth["id"], card_ids)
    return {"status": "ok", "desired_cards": desired}


@app.delete("/me/desired-cards/{card_id}")
async def delete_desired_card(
    card_id: str,
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    get_or_create_user(auth["id"])
    current = [c["id"] for c in get_desired_cards(auth["id"]) if c["id"] != card_id]
    desired = set_desired_cards(auth["id"], current)
    return {"status": "ok", "desired_cards": desired}


@app.delete("/me/collection")
async def clear_my_collection(
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    get_or_create_user(auth["id"])
    clear_user_cards(auth["id"])
    return {"status": "ok", "cards": get_user_cards(auth["id"])}


@app.post("/me/inventory/add-zero")
async def add_zero_inventory_cards(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    """Add selected master cards to the user's inventory with quantity 0."""
    auth = _validate_telegram_init_data(init_data)
    card_ids = data.get("card_ids", [])
    if not isinstance(card_ids, list):
        raise HTTPException(status_code=400, detail="card_ids должен быть массивом")

    get_or_create_user(auth["id"])
    normalized = [str(card_id).strip() for card_id in card_ids if str(card_id).strip()]
    if len(normalized) > 200:
        raise HTTPException(status_code=400, detail="Слишком много карт за один запрос")

    added = add_zero_cards(auth["id"], normalized)
    return {
        "status": "ok",
        "added": added,
        "cards": get_user_cards(auth["id"]),
    }


def _normalize_collection_updates(cards: list[Any]) -> list[tuple[str, int]]:
    """Normalize inventory updates and make duplicate card IDs deterministic.

    A card is represented once in the album and its badge carries the quantity.
    If image detection reports the same stable card ID more than once, keep the
    greatest reported quantity instead of letting the last row silently win.
    Repeating the same scan therefore remains an absolute, idempotent update.
    """
    quantities: dict[str, int] = {}
    for item in cards:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        card_id = str(item["id"]).strip()
        if not card_id:
            continue
        try:
            quantity = max(0, min(999, int(item.get("quantity", 0) or 0)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Некорректное количество карты") from exc
        quantities[card_id] = max(quantities.get(card_id, 0), quantity)
    return list(quantities.items())


@app.post("/me/collection/confirm")
async def confirm_my_collection(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    cards = data.get("cards", [])
    if not isinstance(cards, list):
        raise HTTPException(status_code=400, detail="cards должен быть массивом")
    get_or_create_user(auth["id"])
    updates = _normalize_collection_updates(cards)
    for card_id, quantity in updates:
        set_user_card_quantity(auth["id"], card_id, quantity)
    saved = len(updates)
    return {"status": "ok", "saved": saved, "cards": get_user_cards(auth["id"])}


@app.post("/me/collection")
async def save_my_collection(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    cards = data.get("cards", [])
    if not isinstance(cards, list):
        raise HTTPException(status_code=400, detail="cards должен быть массивом")
    get_or_create_user(auth["id"])
    saved = 0
    for item in cards:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        quantity = max(0, int(item.get("quantity", 0) or 0))
        set_user_card_quantity(auth["id"], str(item["id"]), quantity)
        saved += 1
    return {"status": "ok", "saved": saved, "cards": get_user_cards(auth["id"]) }


@app.post("/scan")
async def scan(file: UploadFile = File(...), init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    auth = _validate_telegram_init_data(init_data) if init_data else None
    image = await _read_image(file)
    result = scan_screenshot(image, save_previews=True)
    db = load_db()

    matches = []
    for card in result["cards"]:
        best_id, best_distance = _match_card(card["hashes"], db)
        raw_id, raw_distance = _match_card(card.get("raw_hashes", {}), db)
        if raw_id is not None and raw_distance < best_distance:
            best_id, best_distance = raw_id, raw_distance
        matched = (
            best_id is not None
            and best_distance <= float(os.getenv("CARD_MATCH_THRESHOLD", "18"))
        )
        if matched:
            info = db[best_id]
            matches.append({
                "id": best_id,
                "name": info.get("name", best_id),
                "rarity": info.get("rarity", 1),
                "distance": round(best_distance, 2),
                "icon": info.get("icon", ""),
                "preview": card.get("preview", ""),
                "quantity": int(card.get("quantity", 1) or 1),
                "duplicates": int(card.get("duplicates", 0) or 0),
                "row": card["row"],
                "col": card["col"],
            })
        else:
            matches.append({
                "id": None,
                "name": "Неизвестная карта",
                "rarity": None,
                "distance": None,
                "icon": "",
                "preview": card.get("preview", ""),
                "quantity": int(card.get("quantity", 1) or 1),
                "duplicates": int(card.get("duplicates", 0) or 0),
                "row": card["row"],
                "col": card["col"],
            })
    if auth:
        get_or_create_user(auth["id"])

    return {"cards": matches, "count": len(matches), "authenticated": bool(auth)}


@app.post("/admin/save-card")
async def save_card(
    data: CardSaveSchema,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    hashes = clean_hashes(data.hashes)
    legacy_hash = data.hash if valid_hash(data.hash) else None

    if not hashes and legacy_hash:
        hashes = {"full_dhash": legacy_hash}

    if not hashes:
        raise HTTPException(status_code=400, detail="Не удалось получить валидные hashes")

    db = load_db()
    card_id = data.id.strip() if data.id else _next_card_id(db)
    old = db.get(card_id, {})
    record = {
        "name": data.name,
        "hashes": hashes,
        "hash": hashes.get("full_dhash") or legacy_hash,
        "rarity": data.rarity,
    }

    # Preserve a previously saved permanent icon when editing the card.
    if old.get("icon"):
        record["icon"] = old["icon"]

    db[card_id] = record
    save_db(db)
    return {
        "status": "ok",
        "card_id": card_id,
        "card": record,
    }


@app.post("/admin/match-icon")
async def match_icon(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    image = await _read_image(file)
    card_image = _extract_single_card(image)
    hashes = calculate_card_hashes(card_image)
    db = load_db()
    card_id, score = _match_card(hashes, db)

    if card_id is None:
        return {
            "matched": False,
            "score": None,
            "hashes": hashes,
            "icon": "",
            "message": "Не удалось найти подходящую карту",
        }

    info = db[card_id]
    return {
        "matched": True,
        "card_id": card_id,
        "name": info.get("name", card_id),
        "rarity": info.get("rarity", 1),
        "score": round(score, 2),
        "hashes": hashes,
        "icon": info.get("icon", ""),
    }


@app.post("/admin/save-icon/{card_id}")
async def save_icon(
    card_id: str,
    file: UploadFile = File(...),
    manual: bool = False,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    image = await _read_image(file)
    card_image = _extract_single_card(image)

    # Always calculate hashes from the actual extracted card. Automatic
    # matching remains strict, but a manual admin confirmation is an explicit
    # identity decision and must also support high-quality viewer screenshots
    # whose pixels differ from the inventory screenshot.
    hashes = calculate_card_hashes(card_image)
    matched_id, score = _match_card(hashes, db)

    if not manual and (matched_id != card_id or score > ICON_MATCH_THRESHOLD):
        raise HTTPException(
            status_code=400,
            detail=f"Изображение не подтверждено для {card_id} (найдено: {matched_id}, distance={score:.2f}, порог={ICON_MATCH_THRESHOLD:g})",
        )

    icon = _prepare_icon(card_image)
    path = CARDS_DIR / f"{card_id}.webp"
    icon.save(path, "WEBP", quality=86, method=6)

    db[card_id]["icon"] = f"/static/cards/{path.name}"
    save_db(db)

    # Manual confirmation may intentionally have no automatic match.
    # Never return infinity to Starlette/JSON: JSON does not allow NaN/Infinity.
    safe_score = round(score, 2) if math.isfinite(score) else None

    return {
        "status": "ok",
        "card_id": card_id,
        "icon": db[card_id]["icon"],
        "score": safe_score,
    }


@app.delete("/admin/delete-icon/{card_id}")
async def delete_icon(
    card_id: str,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    path = CARDS_DIR / f"{card_id}.webp"
    if path.exists():
        path.unlink()

    db[card_id].pop("icon", None)
    save_db(db)
    return {"status": "ok"}


@app.delete("/admin/delete-card/{card_id}")
async def delete_card(
    card_id: str,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    path = CARDS_DIR / f"{card_id}.webp"
    if path.exists():
        path.unlink()

    del db[card_id]
    save_db(db)
    return {"status": "ok"}


def _github_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pubg-card-admin",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GitHub API: {exc.code} {detail[:500]}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API недоступен: {exc}") from exc


def _github_commit() -> dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    branch = os.getenv("GITHUB_BRANCH", "main")

    if not token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN не задан на Render")

    api = f"https://api.github.com/repos/{repo}"
    ref = _github_request("GET", f"{api}/git/ref/heads/{quote(branch, safe='')}", token)
    head_sha = ref["object"]["sha"]

    commit = _github_request("GET", f"{api}/git/commits/{head_sha}", token)
    base_tree = commit["tree"]["sha"]

    # Read the complete current tree so deleted card icons can be removed from
    # GitHub as well.
    tree_data = _github_request(
        "GET",
        f"{api}/git/trees/{head_sha}?recursive=1",
        token,
    )
    existing = {
        item["path"]: item["sha"]
        for item in tree_data.get("tree", [])
        if item.get("type") == "blob"
    }

    blobs = []
    card_json = DB_FILE.read_bytes()
    blobs.append(("card_hashes.json", card_json))

    local_icons = {
        str(p.relative_to(Path("."))).replace("\\", "/"): p
        for p in CARDS_DIR.glob("*.webp")
        if p.is_file()
    }

    for path, local_path in local_icons.items():
        blobs.append((path, local_path.read_bytes()))

    tree_entries: list[dict[str, Any]] = []

    for path, content in blobs:
        blob = _github_request(
            "POST",
            f"{api}/git/blobs",
            token,
            {
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    # Delete old card icons from GitHub when they no longer exist locally.
    for path in existing:
        if path.startswith("static/cards/") and path.endswith(".webp") and path not in local_icons:
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    new_tree = _github_request(
        "POST",
        f"{api}/git/trees",
        token,
        {"base_tree": base_tree, "tree": tree_entries},
    )

    new_commit = _github_request(
        "POST",
        f"{api}/git/commits",
        token,
        {
            "message": "Update PUBG card database and icons",
            "tree": new_tree["sha"],
            "parents": [head_sha],
        },
    )

    _github_request(
        "PATCH",
        f"{api}/git/refs/heads/{quote(branch, safe='')}",
        token,
        {"sha": new_commit["sha"], "force": False},
    )

    return {
        "status": "ok",
        "commit": new_commit["sha"],
        "url": new_commit.get("html_url", ""),
        "cards": len(load_db()),
        "icons": len(local_icons),
    }



def _regenerate_hashes_from_icons() -> None:
    """Rebuild all card hashes from the saved card icons."""
    global HASH_REGEN_PROGRESS

    db = load_db()
    items = [
        (card_id, info)
        for card_id, info in db.items()
        if isinstance(info, dict) and (CARDS_DIR / f"{card_id}.webp").is_file()
    ]

    HASH_REGEN_PROGRESS = {
        "running": True,
        "current": 0,
        "total": len(items),
        "card_id": "",
        "status": "running",
        "updated": int(time.time()),
        "error": "",
    }

    try:
        for index, (card_id, info) in enumerate(items, start=1):
            icon_path = CARDS_DIR / f"{card_id}.webp"

            with Image.open(icon_path) as icon:
                hashes = calculate_card_hashes(icon.convert("RGB"))

            info["hashes"] = hashes
            info["hash"] = hashes.get("full_dhash", info.get("hash", ""))

            HASH_REGEN_PROGRESS.update({
                "current": index,
                "total": len(items),
                "card_id": card_id,
                "status": "running" if index < len(items) else "completed",
                "updated": int(time.time()),
            })

        save_db(db)

    except Exception as exc:
        HASH_REGEN_PROGRESS.update({
            "status": "error",
            "error": str(exc),
            "updated": int(time.time()),
        })
    finally:
        HASH_REGEN_PROGRESS["running"] = False


@app.post("/admin/regenerate-hashes")
async def regenerate_hashes(authorization: str | None = Header(default=None)):
    require_admin(authorization)

    if HASH_REGEN_PROGRESS.get("running"):
        return HASH_REGEN_PROGRESS

    # Start in a background thread so the admin page can poll progress.
    import threading
    thread = threading.Thread(
        target=_regenerate_hashes_from_icons,
        name="hash-regeneration",
        daemon=True,
    )
    thread.start()

    return {
        "running": True,
        "current": 0,
        "total": sum(
            1 for card_id, info in load_db().items()
            if isinstance(info, dict) and (CARDS_DIR / f"{card_id}.webp").is_file()
        ),
        "card_id": "",
        "status": "starting",
        "updated": int(time.time()),
        "error": "",
    }


@app.get("/admin/regenerate-hashes/progress")
async def regenerate_hashes_progress(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return HASH_REGEN_PROGRESS


@app.post("/admin/github-commit")
async def github_commit(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return _github_commit()
