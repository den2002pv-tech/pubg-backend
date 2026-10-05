from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
import imagehash
import cv2
import numpy as np
import json
import io
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Загружаем базу хэшей
HASHES_FILE = "card_hashes.json"
if os.path.exists(HASHES_FILE):
    with open(HASHES_FILE, "r", encoding="utf-8") as f:
        CARD_DB = json.load(f)
else:
    CARD_DB = {}

def process_and_hash(pil_img):
    """Препроцессинг и снятие dhash с изображения"""
    img = pil_img.convert('L')
    img = img.resize((128, 128), Image.Resampling.LANCZOS)
    return str(imagehash.dhash(img))

def find_matching_card(crop_hash):
    """Сравнение хэша с базой по расстоянию Хэмминга"""
    best_match = None
    min_diff = 15  # Порог погрешности
    
    crop_h = imagehash.hex_to_hash(crop_hash)
    
    for card_id, data in CARD_DB.items():
        db_h = imagehash.hex_to_hash(data["hash"])
        diff = crop_h - db_h
        if diff < min_diff:
            min_diff = diff
            best_match = data["name"]
            
    return best_match

@app.get("/")
def home():
    return {"status": "PUBG Cards API Active"}

@app.post("/scan")
async def scan_screenshot(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        # Нормализация по высоте
        h, w = img.shape[:2]
        scale = 720.0 / h
        img_resized = cv2.resize(img, (int(w * scale), 720))
        
        # Поиск контуров карт
        gray = cv2.cvtColor(img_resized, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        detected_cards = []
        
        for cnt in contours:
            x, y, card_w, card_h = cv2.boundingRect(cnt)
            aspect_ratio = card_h / float(card_w) if card_w > 0 else 0
            
            # Фильтр рамок карт
            if 150 < card_h < 400 and 1.1 < aspect_ratio < 1.6:
                art_y1, art_y2 = int(y + card_h * 0.15), int(y + card_h * 0.70)
                art_x1, art_x2 = int(x + card_w * 0.10), int(x + card_w * 0.90)
                
                crop = img_resized[art_y1:art_y2, art_x1:art_x2]
                if crop.size == 0:
                    continue
                    
                crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                pil_crop = Image.fromarray(crop_rgb)
                
                crop_hash = process_and_hash(pil_crop)
                match_name = find_matching_card(crop_hash)
                
                if match_name and match_name not in detected_cards:
                    detected_cards.append(match_name)
        
        return {"status": "success", "cards": detected_cards}
        
    except Exception as e:
        return {"status": "error", "message": str(e)}
