import base64
import json
import os
import time
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel


app = FastAPI()

# Разрешаем CORS для связи с фронтендом на Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Папка для временных превью картинок
TEMP_DIR = "temp_images"
os.makedirs(TEMP_DIR, exist_ok=True)
app.mount(
    "/temp_images", StaticFiles(directory=TEMP_DIR), name="temp_images"
)

# Путь к JSON базе данных
DB_FILE = "card_hashes.json"


def load_db():
  if os.path.exists(DB_FILE):
    with open(DB_FILE, "r", encoding="utf-8") as f:
      try:
        return json.load(f)
      except:
        return {}
  return {}


def save_db(data):
  with open(DB_FILE, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)


def compute_dhash(image: Image.Image, hash_size=8):
  """Упрощенное вычисление dhash для карточки"""
  img = image.convert("L").resize(
      (hash_size + 1, hash_size), Image.Resampling.LANCZOS
  )
  pixels = list(img.getdata())
  difference = []
  for row in range(hash_size):
    row_start = row * (hash_size + 1)
    for col in range(hash_size):
      left = pixels[row_start + col]
      right = pixels[row_start + col + 1]
      difference.append(pixels[row_start + col] > pixels[row_start + col + 1])

  decimal_value = 0
  for index, value in enumerate(difference):
    if value:
      decimal_value += 1 << index
  return f"{decimal_value:016x}"


class CardSaveSchema(BaseModel):
  id: str
  name: str
  hash: str
  rarity: int
  preview: str = ""


# --- Раздача статических HTML-страниц ---


@app.get("/")
async def serve_index():
  return FileResponse("index.html")


@app.get("/admin")
async def serve_admin():
  return FileResponse("admin.html")


# --- API Эндпоинты ---


@app.post("/admin/scan-preview")
async def scan_preview(file: UploadFile = File(...)):
  try:
    image_bytes = await file.read()
    from io import BytesIO

    pil_img = Image.open(BytesIO(image_bytes)).convert("RGB")
  except Exception:
    raise HTTPException(
        status_code=400, detail="Не удалось прочитать изображение"
    )

  w, h = pil_img.size
  aspect_ratio = w / float(h)
  cards = []

  if aspect_ratio > 1.35:
    # Настройки сетки под нижний ряд карт инвентаря PUBG Mobile
    grid_left = w * 0.130
    grid_top = h * 0.330
    grid_right = w * 0.795
    grid_bottom = h * 0.850

    grid_w = grid_right - grid_left
    grid_h = grid_bottom - grid_top

    cols = 4
    rows = 1

    cell_w = grid_w / cols
    cell_h = grid_h / rows

    for c in range(cols):
      x1 = grid_left + c * cell_w
      y1 = grid_top
      x2 = x1 + cell_w
      y2 = grid_top + cell_h

      pad_x = cell_w * 0.08
      pad_top = cell_h * 0.08
      pad_bottom = cell_h * 0.05

      art_x1 = int(x1 + pad_x)
      art_y1 = int(y1 + pad_top)
      art_x2 = int(x2 - pad_x)
      art_y2 = int(y2 - pad_bottom)

      card_crop = pil_img.crop((art_x1, art_y1, art_x2, art_y2))

      # Сохраняем временную иконку на диск для проверки в админке
      filename = f"card_{c}_{int(time.time())}.png"
      filepath = os.path.join(TEMP_DIR, filename)
      card_crop.save(filepath)

      preview_url = f"/temp_images/{filename}"
      card_hash = compute_dhash(card_crop)

      cards.append({"preview": preview_url, "hash": card_hash})

  return {"cards": cards}


@app.get("/cards")
async def get_cards_endpoint():
  return load_db()


@app.post("/admin/save-card")
async def save_card(data: CardSaveSchema):
  db = load_db()
  db[data.id] = {
      "name": data.name,
      "hash": data.hash,
      "rarity": data.rarity,
      "image": data.preview,  # Сохраняем ссылку на превью или саму иконку
  }
  save_db(db)
  return {"status": "ok"}


@app.delete("/admin/delete-card/{card_id}")
async def delete_card(card_id: str):
  db = load_db()
  if card_id in db:
    del db[card_id]
    save_db(db)
    return {"status": "ok"}
  raise HTTPException(status_code=404, detail="Карта не найдена")
