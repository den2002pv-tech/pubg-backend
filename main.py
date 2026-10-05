import io
import json
import base64
import os
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

# Импортируем сканер из нашего файла
try:
    from scanner import scan_screenshot
except ImportError:
    # Заглушка на случай, если файл scanner.py не найдется
    def scan_screenshot(pil_img):
        return []

DB_FILE = "card_hashes.json"

def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Ошибка чтения {DB_FILE}: {e}")
        return {}

def save_db(data: dict):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения в {DB_FILE}: {e}")

def dhash(image: Image.Image, hash_size: int = 8) -> str:
    image = image.convert('L').resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    
    difference = []
    for row in range(hash_size):
        for col in range(hash_size):
            pixel_left = pixels[row * (hash_size + 1) + col]
            pixel_right = pixels[row * (hash_size + 1) + col + 1]
            difference.append(pixel_left > pixel_right)
            
    decimal_value = 0
    hex_string = []
    for i, value in enumerate(difference):
        if value:
            decimal_value += 2 ** (i % 8)
        if (i % 8) == 7:
            hex_string.append(hex(decimal_value)[2:].zfill(2))
            decimal_value = 0
            
    return ''.join(hex_string)

def process_and_hash(pil_img: Image.Image) -> str:
    w, h = pil_img.size
    crop_box = (int(w * 0.10), int(h * 0.15), int(w * 0.90), int(h * 0.70))
    cropped_art = pil_img.crop(crop_box)
    return dhash(cropped_art)

def hamming_distance(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count('1')

def image_to_base64(pil_img: Image.Image) -> str:
    buffered = io.BytesIO()
    pil_img.save(buffered, format="JPEG", quality=80)
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{img_str}"

def check_duplicate(new_hash: str, db: dict, threshold: int = 5):
    for card_id, card_data in db.items():
        existing_hash = ""
        card_name = card_id

        if isinstance(card_data, dict):
            existing_hash = card_data.get("hash", "")
            card_name = card_data.get("name", card_id)
        elif isinstance(card_data, str):
            existing_hash = card_data

        if existing_hash:
            try:
                diff = hamming_distance(new_hash, existing_hash)
                if diff <= threshold:
                    return f"{card_name} (ID: {card_id})"
            except Exception:
                continue
    return None

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "PUBG Card Scanner API is running!"}

@app.get("/cards")
def get_cards():
    return load_db()

@app.post("/admin/scan-preview")
async def scan_preview(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        pil_img = Image.open(io.BytesIO(contents)).convert('RGB')
        db = load_db()
        
        # Запускаем поиск карточек через OpenCV
        detected = scan_screenshot(pil_img)
        
        # Если OpenCV ничего не нашел (например, загружена 1 вырезанная карта), берем всё фото
        if not detected:
            detected = [{'crop': pil_img}]
            
        results = []
        for item in detected:
            crop_img = item.get('crop')
            card_hash = process_and_hash(crop_img)
            b64_img = image_to_base64(crop_img)
            match_name = check_duplicate(card_hash, db)
            
            results.append({
                "hash": card_hash,
                "preview_b64": b64_img,
                "existing_match": match_name
            })

        return {"status": "success", "count": len(results), "cards": results}

    except Exception as e:
        print(f"Ошибка в /admin/scan-preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/save-card")
async def save_card(
    card_id: str = Form(...),
    name: str = Form(...),
    rarity: int = Form(...),
    card_hash: str = Form(...)
):
    try:
        db = load_db()
        db[card_id] = {
            "name": name,
            "hash": card_hash,
            "rarity": int(rarity)
        }
        save_db(db)
        return {"status": "success", "message": f"Карта '{name}' сохранена!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/admin/delete-card/{card_id}")
async def delete_card(card_id: str):
    try:
        db = load_db()
        if card_id in db:
            del db[card_id]
            save_db(db)
            return {"status": "success", "message": f"Карта '{card_id}' удалена!"}
        return {"status": "error", "message": "Карта не найдена"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
