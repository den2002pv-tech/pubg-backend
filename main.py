from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scanner import match_scan_results, scan_screenshot


app = FastAPI(title="PUBG Card Scanner")

TEMP_DIR = Path("temp_images")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = Path("card_hashes.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/temp_images", StaticFiles(directory=TEMP_DIR), name="temp_images")


def load_db() -> dict:
    if not DB_FILE.exists():
        return {}

    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_db(data: dict) -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix="card_hashes_",
        suffix=".json",
        dir=str(DB_FILE.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

        os.replace(tmp_name, DB_FILE)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class CardSaveSchema(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    rarity: int = Field(ge=1, le=3)
    preview: str = ""
    hashes: dict[str, str] | None = None
    hash: str | None = None


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/cards")
def get_cards():
    return load_db()


@app.post("/scan")
async def scan(file: UploadFile = File(...)):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="Empty image")

    try:
        result = scan_screenshot(content, save_previews=False, save_debug=False)
        return match_scan_results(result, load_db())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Scan failed: {exc}") from exc


@app.post("/admin/scan-preview")
async def admin_scan_preview(file: UploadFile = File(...)):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="Empty image")

    try:
        return scan_screenshot(content, save_previews=True, save_debug=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Scan failed: {exc}") from exc


@app.post("/admin/save-card")
def save_card(data: CardSaveSchema):
    database = load_db()

    hashes = data.hashes or {}
    if not hashes and data.hash:
        hashes = {"full_dhash": data.hash}

    database[data.id] = {
        "name": data.name,
        "hashes": hashes,
        "hash": data.hash or hashes.get("full_dhash", ""),
        "rarity": data.rarity,
        "image": data.preview,
    }

    save_db(database)

    return {
        "ok": True,
        "id": data.id,
        "card": database[data.id],
    }


@app.delete("/admin/delete-card/{card_id}")
def delete_card(card_id: str):
    database = load_db()

    if card_id not in database:
        raise HTTPException(status_code=404, detail="Card not found")

    del database[card_id]
    save_db(database)

    return {"ok": True, "id": card_id}
