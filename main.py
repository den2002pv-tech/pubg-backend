from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from pydantic import BaseModel

from scanner import calculate_card_hashes, detect_inventory_cards, scan_screenshot
from database import clear_user_cards, get_or_create_user, get_user_cards, init_db, set_user_card_quantity, sync_cards

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
CARD_MATCH_STRONG_DISTANCE = int(os.getenv("CARD_MATCH_STRONG_DISTANCE", "5"))
CARD_MATCH_ACCEPTABLE_DISTANCE = int(os.getenv("CARD_MATCH_ACCEPTABLE_DISTANCE", "10"))
CARD_MATCH_MIN_STRONG_HASHES = int(os.getenv("CARD_MATCH_MIN_STRONG_HASHES", "2"))
CARD_MATCH_MIN_ACCEPTABLE_HASHES = int(os.getenv("CARD_MATCH_MIN_ACCEPTABLE_HASHES", "3"))
CARD_MATCH_COLOR_MAX_RATIO = float(os.getenv("CARD_MATCH_COLOR_MAX_RATIO", "0.20"))


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
    Match by hash voting instead of a global average.

    A card is accepted only when:
      - at least 2 of the 4 grayscale hashes are <= 5 bits apart;
      - at least 3 of the 4 grayscale hashes are <= 10 bits apart;
      - no available grayscale hash is above 10;
      - when a frame color hash exists, it must also be compatible.

    The returned score is the average of the two strongest grayscale hashes.
    This keeps the displayed distance intuitive while preventing several
    mediocre 10-18 distances from producing a false match.
    """
    best_id: str | None = None
    best_score = float("inf")

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

        # Legacy records may only have the old single "hash" field.
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

        if strong_count < CARD_MATCH_MIN_STRONG_HASHES:
            continue

        if acceptable_count < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            continue

        # No individual grayscale hash may be a loose 10-18 match.
        if distances[-1] > CARD_MATCH_ACCEPTABLE_DISTANCE:
            continue

        scan_color = card_hashes.get("frame_colorhash")
        stored_color = stored.get("frame_colorhash")

        # If the scanner has a color frame hash, a stored card without one
        # cannot be considered an identity match.
        if scan_color and not stored_color:
            continue

        if scan_color and stored_color:
            color_distance = _hamming(scan_color, stored_color)
            if color_distance is None:
                continue

            color_bits = max(1, len(scan_color) * 4)
            if (color_distance / color_bits) > CARD_MATCH_COLOR_MAX_RATIO:
                continue

        score = (distances[0] + distances[1]) / 2
        if score < best_score:
            best_score = score
            best_id = card_id

    return best_id, best_score

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
    image = await _read_image(file)
    result = scan_screenshot(image, save_previews=True)
    for index, card in enumerate(result.get("cards", [])):
        if card.get("preview"):
            card["preview"] = _save_admin_preview(card["preview"], index)
    db = load_db()
    result["cards"] = _enrich_admin_scan(
        result.get("cards", []),
        db,
    )
    return result


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
    }


@app.delete("/me/collection")
async def clear_my_collection(
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    get_or_create_user(auth["id"])
    deleted = clear_user_cards(auth["id"])
    return {"status": "ok", "deleted": deleted, "cards": []}


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
    saved = 0
    for item in cards:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        quantity = max(0, int(item.get("quantity", 0) or 0))
        set_user_card_quantity(auth["id"], str(item["id"]), quantity)
        saved += 1
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
