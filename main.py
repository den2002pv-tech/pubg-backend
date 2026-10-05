import io
import json
import base64
import os
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

# Импортируем ваши функции из модуля сканера (или замените имя файла на ваш модуль)
try:
    from scanner import scan_screenshot, process_and_hash, load_db, save_db, hamming_distance
except ImportError:
    # Если функции лежат в самом main.py или другом файле, убедитесь в правильности импорта
    pass

app = FastAPI()

# Включаем CORS, чтобы Vercel мог свободно делать запросы к Render
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Заглушка для Health Check Render
@app.get("/")
def read_root():
    return {"status": "ok", "message": "PUBG Card Scanner API is running!"}

@app.get("/cards")
def get_cards():
    return load_db()

def image_to_base64(pil_img: Image.Image) -> str:
    """Преобразует PIL картинку в Base64 для передачи на фронтенд"""
    buffered = io.BytesIO()
    pil_img.save(buffered, format="JPEG", quality=80)
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{img_str}"

def check_duplicate(new_hash: str, db: dict, threshold: int = 5):
    """Проверка дубликатов по расстоянию Хэмминга"""
    for card_id, card_data in db.items():
        existing_hash = card_data.get("hash", "")
        if existing_hash:
            try:
                # Если функция hamming_distance импортирована
                diff = hamming_distance(new_hash, existing_hash)
            except NameError:
                # Резервный расчет расстояния Хэмминга для 16-ричных строк
                diff = bin(int(new_hash, 16) ^ int(existing_hash, 16)).count('1')
            
            if diff <= threshold:
                return f"{card_data.get('name', card_id)} (ID: {card_id})"
    return None

@app.post("/admin/scan-preview")
async def scan_preview(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        pil_img = Image.open(io.BytesIO(contents)).convert('RGB')
        
        db = load_db()
        detected_cards = []
        
        try:
            detected_cards = scan_screenshot(pil_img)
        except Exception as scan_err:
            print(f"Ошибка автосканера: {scan_err}")

        # Если сканер ничего не нашел (загружена 1 вырезанная карточка)
        if not detected_cards:
            w, h = pil_img.size
            crop_box = (int(w * 0.10), int(h * 0.15), int(w * 0.90), int(h * 0.70))
            cropped_art = pil_img.crop(crop_box)
            card_hash = process_and_hash(cropped_art)
            b64_img = image_to_base64(pil_img)
            
            return {
                "status": "success",
                "count": 1,
                "cards": [{
                    "hash": card_hash,
                    "preview_b64": b64_img,
                    "existing_match": check_duplicate(card_hash, db)
                }]
            }
            
        results = []
        for card_data in detected_cards:
            crop_img = card_data.get('crop')
            card_hash = card_data.get('hash', '')
            
            b64_img = image_to_base64(crop_img) if crop_img else ""
            match_name = check_duplicate(card_hash, db) if card_hash else None
            
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
        
