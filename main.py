import io
import json
import base64
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from PIL import Image

# Импортируйте ваши функции сканера и работы с базой
# from scanner import scan_screenshot, process_and_hash, load_db, save_db, hamming_distance

app = FastAPI()

def image_to_base64(pil_img: Image.Image) -> str:
    """Преобразует PIL картинку в Base64 строку для временного показа в админке"""
    buffered = io.BytesIO()
    pil_img.save(buffered, format="JPEG", quality=80)
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{img_str}"


@app.post("/admin/scan-preview")
async def scan_preview(file: UploadFile = File(...)):
    """
    1. Принимает скриншот.
    2. Сканирует и вырезает карты.
    3. Возвращает временный Base64 (для отрисовки в браузере) и хэш.
    Никакие файлы НЕ сохраняются на диск.
    """
    try:
        contents = await file.read()
        pil_img = Image.open(io.BytesIO(contents)).convert('RGB')
        
        detected_cards = scan_screenshot(pil_img)
        db = load_db()
        
        # Если сканер ничего не нашел, считаем что загружена 1 обрезаная карта
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
            card_hash = card_data['hash']
            
            b64_img = image_to_base64(crop_img) if crop_img else ""
            
            # Проверяем, есть ли уже похожая карта в базе
            match_name = check_duplicate(card_hash, db)
            
            results.append({
                "hash": card_hash,
                "preview_b64": b64_img,
                "existing_match": match_name  # Покажет имя, если карта уже занесена
            })

        return {"status": "success", "count": len(results), "cards": results}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def check_duplicate(new_hash: str, db: dict, threshold: int = 5):
    """Вспомогательная функция: проверяет, есть ли похожий хэш в базе"""
    for card_id, card_data in db.items():
        existing_hash = card_data.get("hash", "")
        if existing_hash:
            # Расстояние Хэмминга между хэшами
            diff = hamming_distance(new_hash, existing_hash)
            if diff <= threshold:
                return f"{card_data['name']} (ID: {card_id})"
    return None


@app.post("/admin/save-card")
async def save_card(
    card_id: str = Form(...),
    name: str = Form(...),
    rarity: int = Form(...),
    card_hash: str = Form(...)
):
    """Сохраняет в JSON только чистые данные (без картинок)"""
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
