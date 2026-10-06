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

from fastapi import Cookie, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from pydantic import BaseModel

from scanner import calculate_card_hashes, detect_inventory_cards, scan_screenshot

app = FastAPI(title="PUBG Card Scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = Path("temp_images")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path("static")
CARDS_DIR = STATIC_DIR / "cards"
CARDS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/temp_images", StaticFiles(directory=str(TEMP_DIR)), name="temp_images")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

DB_FILE = Path("card_hashes.json")
ADMIN_COOKIE = "pubg_admin"
SESSION_MAX_AGE = 12 * 60 * 60


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


def local_preview_path(url: str) -> Path | None:
    if not url:
        return None
    prefix = "/temp_images/"
    if not url.startswith(prefix):
        return None
    name = Path(url[len(prefix):]).name
    path = (TEMP_DIR / name).resolve()
    if path.parent != TEMP_DIR.resolve() or not path.exists():
        return None
    return path


def _session_secret() -> str:
    secret = os.getenv("ADMIN_SESSION_SECRET", "")
    if not secret:
        # Works for a prototype, but set ADMIN_SESSION_SECRET on Render so
        # sessions survive a restart.
        secret = os.getenv("ADMIN_PASSWORD", "change-me")
    return secret


def _make_session() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(
        _session_secret().encode("utf-8"),
        ts.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{ts}.{sig}"


def _valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    ts_s, sig = value.split(".", 1)
    try:
        ts = int(ts_s)
    except ValueError:
        return False
    if time.time() - ts > SESSION_MAX_AGE or ts > time.time() + 60:
        return False
    expected = hmac.new(
        _session_secret().encode("utf-8"),
        ts_s.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def require_admin(admin_cookie: str | None) -> None:
    if not os.getenv("ADMIN_PASSWORD"):
        raise HTTPException(
            status_code=503,
            detail="На Render не задан ADMIN_PASSWORD",
        )
    if not _valid_session(admin_cookie):
        raise HTTPException(status_code=401, detail="Требуется вход в админку")


class LoginSchema(BaseModel):
    password: str


class CardSaveSchema(BaseModel):
    id: str
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


ICON_MATCH_THRESHOLD = float(os.getenv("ICON_MATCH_THRESHOLD", "18"))


def _match_card(card_hashes: dict[str, str], db: dict[str, Any]) -> tuple[str | None, float]:
    """
    Compare corresponding hashes. Lower is better.
    Each available hash contributes equally; this avoids comparing a dHash
    against a pHash just because both are hex strings.
    """
    best_id: str | None = None
    best_score = float("inf")

    for card_id, info in db.items():
        stored = clean_hashes(info.get("hashes"))
        distances: list[int] = []

        for key, value in card_hashes.items():
            other = stored.get(key)
            if other:
                d = _hamming(value, other)
                if d is not None:
                    distances.append(d)

        if not distances:
            legacy = info.get("hash")
            d = _hamming(card_hashes.get("full_dhash", ""), str(legacy)) if legacy else None
            if d is not None:
                distances.append(d)

        if distances:
            # Mean of the corresponding hashes. Four hashes make recognition
            # more stable than relying on only one.
            score = sum(distances) / len(distances)
            if score < best_score:
                best_score = score
                best_id = card_id

    return best_id, best_score


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
async def get_cards_endpoint(admin_cookie: str | None = Cookie(default=None)):
    require_admin(admin_cookie)
    return load_db()


@app.get("/admin/status")
async def admin_status(admin_cookie: str | None = Cookie(default=None)):
    return {
        "logged_in": _valid_session(admin_cookie),
        "cards": len(load_db()),
        "icons": sum(1 for x in load_db().values() if x.get("icon")),
        "temporary_files": sum(1 for p in TEMP_DIR.iterdir() if p.is_file()),
    }


@app.post("/admin/login")
async def admin_login(data: LoginSchema, response: Response):
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD не задан на Render")
    if not hmac.compare_digest(data.password, password):
        raise HTTPException(status_code=401, detail="Неверный пароль")

    response.set_cookie(
        ADMIN_COOKIE,
        _make_session(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return {"status": "ok"}


@app.post("/admin/logout")
async def admin_logout(response: Response):
    response.delete_cookie(ADMIN_COOKIE)
    return {"status": "ok"}


@app.post("/admin/scan-preview")
async def scan_preview(
    file: UploadFile = File(...),
    admin_cookie: str | None = Cookie(default=None),
):
    require_admin(admin_cookie)
    image = await _read_image(file)
    return scan_screenshot(image, save_previews=True)


@app.post("/scan")
async def scan(file: UploadFile = File(...)):
    image = await _read_image(file)
    result = scan_screenshot(image, save_previews=False)
    db = load_db()

    matches = []
    for card in result["cards"]:
        best_id, best_distance = _match_card(card["hashes"], db)
        if best_id is not None:
            info = db[best_id]
            matches.append({
                "id": best_id,
                "name": info.get("name", best_id),
                "rarity": info.get("rarity", 1),
                "distance": round(best_distance, 2),
                "icon": info.get("icon", ""),
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
                "row": card["row"],
                "col": card["col"],
            })
    return {"cards": matches, "count": len(matches)}


@app.post("/admin/save-card")
async def save_card(
    data: CardSaveSchema,
    admin_cookie: str | None = Cookie(default=None),
):
    require_admin(admin_cookie)

    hashes = clean_hashes(data.hashes)
    legacy_hash = data.hash if valid_hash(data.hash) else None

    if not hashes:
        path = local_preview_path(data.preview)
        if path:
            try:
                with Image.open(path) as img:
                    hashes = calculate_card_hashes(img.convert("RGB"))
            except Exception:
                hashes = {}

    if not hashes and legacy_hash:
        hashes = {"full_dhash": legacy_hash}

    if not hashes:
        raise HTTPException(status_code=400, detail="Не удалось получить валидные hashes")

    db = load_db()
    old = db.get(data.id, {})
    record = {
        "name": data.name,
        "hashes": hashes,
        "hash": hashes.get("full_dhash") or legacy_hash,
        "rarity": data.rarity,
    }

    # Preserve a previously saved permanent icon when editing the card.
    if old.get("icon"):
        record["icon"] = old["icon"]

    db[data.id] = record
    save_db(db)
    return {"status": "ok", "card": db[data.id]}


@app.post("/admin/match-icon")
async def match_icon(
    file: UploadFile = File(...),
    admin_cookie: str | None = Cookie(default=None),
):
    require_admin(admin_cookie)

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
    }


@app.post("/admin/save-icon/{card_id}")
async def save_icon(
    card_id: str,
    file: UploadFile = File(...),
    admin_cookie: str | None = Cookie(default=None),
):
    require_admin(admin_cookie)

    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    image = await _read_image(file)
    card_image = _extract_single_card(image)

    # Recalculate hashes from the actual card and require a reasonably close
    # match before permanently attaching the icon.
    hashes = calculate_card_hashes(card_image)
    matched_id, score = _match_card(hashes, db)
    if matched_id != card_id or score > ICON_MATCH_THRESHOLD:
        raise HTTPException(
            status_code=400,
            detail=f"Изображение не подтверждено для {card_id} (найдено: {matched_id}, distance={score:.2f}, порог={ICON_MATCH_THRESHOLD:g})",
        )

    icon = _prepare_icon(card_image)
    path = CARDS_DIR / f"{card_id}.webp"
    icon.save(path, "WEBP", quality=86, method=6)

    db[card_id]["icon"] = f"/static/cards/{path.name}"
    save_db(db)

    return {
        "status": "ok",
        "card_id": card_id,
        "icon": db[card_id]["icon"],
        "score": round(score, 2),
    }


@app.delete("/admin/delete-icon/{card_id}")
async def delete_icon(
    card_id: str,
    admin_cookie: str | None = Cookie(default=None),
):
    require_admin(admin_cookie)
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
    admin_cookie: str | None = Cookie(default=None),
):
    require_admin(admin_cookie)
    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    path = CARDS_DIR / f"{card_id}.webp"
    if path.exists():
        path.unlink()

    del db[card_id]
    save_db(db)
    return {"status": "ok"}


@app.get("/admin/temp-files")
async def temp_files(admin_cookie: str | None = Cookie(default=None)):
    require_admin(admin_cookie)
    files = []
    for p in sorted(TEMP_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            files.append({
                "name": p.name,
                "url": f"/temp_images/{p.name}",
                "size": p.stat().st_size,
            })
    return files


@app.delete("/admin/temp-files")
async def delete_temp_files(admin_cookie: str | None = Cookie(default=None)):
    require_admin(admin_cookie)
    count = 0
    for p in TEMP_DIR.iterdir():
        if p.is_file():
            p.unlink()
            count += 1
    return {"status": "ok", "deleted": count}


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


@app.post("/admin/github-commit")
async def github_commit(admin_cookie: str | None = Cookie(default=None)):
    require_admin(admin_cookie)
    return _github_commit()
