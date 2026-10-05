from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scanner import TEMP_DIR, calculate_card_hashes, load_image, match_scan_results, scan_screenshot

DB_PATH = Path("card_hashes.json")

app = FastAPI(title="PUBG Card Scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/temp_images", StaticFiles(directory=str(TEMP_DIR)), name="temp_images")


def load_db() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {}
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_db(data: dict[str, Any]) -> None:
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DB_PATH)


def clean_hashes(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        value = {"full_dhash": value}
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() in {"undefined", "null", "none"}:
            continue
        out[str(k)] = s
    return out


def hashes_from_local_preview(preview: str | None) -> dict[str, str]:
    if not preview or not preview.startswith("/temp_images/"):
        return {}
    name = Path(preview).name
    path = (TEMP_DIR / name).resolve()
    if path.parent != TEMP_DIR.resolve() or not path.exists():
        return {}
    try:
        return calculate_card_hashes(load_image(path))
    except Exception:
        return {}


class CardSaveSchema(BaseModel):
    id: str
    name: str
    rarity: int = 0
    preview: str | None = None
    hashes: dict[str, str] | None = None
    # Legacy field kept so an old admin page cannot silently break saving.
    hash: str | None = None


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cards")
def get_cards():
    return load_db()


@app.post("/scan")
async def scan(file: UploadFile = File(...)):
    data = await file.read()
    scan_result = scan_screenshot(data, save_previews=True, save_debug=True)
    db = load_db()
    return {**scan_result, **match_scan_results(scan_result, db)}


@app.post("/admin/scan-preview")
async def admin_scan_preview(file: UploadFile = File(...)):
    data = await file.read()
    return scan_screenshot(data, save_previews=True, save_debug=True)


@app.post("/admin/save-card")
def save_card(card: CardSaveSchema):
    hashes = clean_hashes(card.hashes)

    # If the browser sends an old single "hash" field, preserve it.
    if not hashes and card.hash:
        hashes = clean_hashes(card.hash)

    # Server-side fallback: calculate hashes from the saved preview.
    # This also fixes mixed/cached frontend/backend versions.
    if not hashes:
        hashes = hashes_from_local_preview(card.preview)

    if not hashes:
        raise HTTPException(
            status_code=400,
            detail="Не удалось получить хеши карточки. Пересканируй изображение."
        )

    data = load_db()
    data[card.id] = {
        "name": card.name,
        "hashes": hashes,
        "hash": hashes.get("full_dhash", next(iter(hashes.values()))),
        "rarity": int(card.rarity),
        "image": card.preview,
    }
    save_db(data)
    return {"ok": True, "card": data[card.id]}


@app.delete("/admin/delete-card/{card_id}")
def delete_card(card_id: str):
    data = load_db()
    if card_id not in data:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    del data[card_id]
    save_db(data)
    return {"ok": True}
