from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from scanner import calculate_card_hashes, scan_screenshot

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
app.mount("/temp_images", StaticFiles(directory=str(TEMP_DIR)), name="temp_images")
DB_FILE = Path("card_hashes.json")


def load_db() -> dict[str, Any]:
    if not DB_FILE.exists():
        return {}
    try:
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_db(data: dict[str, Any]) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_FILE)


def clean_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, val in value.items():
        if val is None:
            continue
        s = str(val).strip()
        if not s or s.lower() == "undefined" or s.lower() == "null":
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


class CardSaveSchema(BaseModel):
    id: str
    name: str
    rarity: int = 1
    preview: str = ""
    hash: str | None = None
    hashes: dict[str, str] | None = None


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
async def get_cards_endpoint():
    return load_db()


async def _read_image(file: UploadFile) -> Image.Image:
    try:
        data = await file.read()
        return Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Не удалось прочитать изображение") from exc


@app.post("/admin/scan-preview")
async def scan_preview(file: UploadFile = File(...)):
    image = await _read_image(file)
    return scan_screenshot(image, save_previews=True)


@app.post("/scan")
async def scan(file: UploadFile = File(...)):
    image = await _read_image(file)
    result = scan_screenshot(image, save_previews=False)
    db = load_db()

    matches = []
    for card in result["cards"]:
        best_id = None
        best_distance = 999
        ch = card["hashes"]
        for card_id, info in db.items():
            stored = clean_hashes(info.get("hashes"))
            legacy = info.get("hash")
            candidates = []
            if stored:
                candidates.extend(stored.values())
            if valid_hash(legacy):
                candidates.append(str(legacy))
            if not candidates:
                continue
            for candidate in candidates:
                try:
                    a = int(ch["full_dhash"], 16)
                    b = int(candidate, 16)
                    distance = (a ^ b).bit_count()
                except Exception:
                    continue
                if distance < best_distance:
                    best_distance = distance
                    best_id = card_id
        if best_id is not None:
            info = db[best_id]
            matches.append({
                "id": best_id,
                "name": info.get("name", best_id),
                "rarity": info.get("rarity", 1),
                "distance": best_distance,
                "row": card["row"],
                "col": card["col"],
            })
        else:
            matches.append({
                "id": None,
                "name": "Неизвестная карта",
                "rarity": None,
                "distance": None,
                "row": card["row"],
                "col": card["col"],
            })
    return {"cards": matches, "count": len(matches)}


@app.post("/admin/save-card")
async def save_card(data: CardSaveSchema):
    hashes = clean_hashes(data.hashes)
    legacy_hash = data.hash if valid_hash(data.hash) else None

    # Server-side recovery: never save "undefined". If the frontend sent only
    # the preview, calculate the hashes again on the backend.
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
        raise HTTPException(status_code=400, detail="Не удалось получить валидный hash для карточки")

    db = load_db()
    db[data.id] = {
        "name": data.name,
        "hashes": hashes,
        "hash": hashes.get("full_dhash") or legacy_hash,
        "rarity": data.rarity,
        "image": data.preview,
    }
    save_db(db)
    return {"status": "ok", "card": db[data.id]}


@app.delete("/admin/delete-card/{card_id}")
async def delete_card(card_id: str):
    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")
    del db[card_id]
    save_db(db)
    return {"status": "ok"}
