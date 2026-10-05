from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
import imagehash
import json
import io
import os

app = FastAPI()

# Настройка CORS для работы с Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Загружаем базу хэшей карт
HASHES_FILE = "card_hashes.json"
if os.path.exists(HASHES_FILE):
    with open(HASHES_FILE, "r") as f:
        CARD_DATABASE = json.load(f)
else:
    CARD_DATABASE = {}

@app.get("/")
def read_root():
    return {"status": "PUBG Card Exchange API is running"}

@app.post("/scan")
async def scan_screenshot(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        img = Image.open(io.BytesIO(contents))
        
        # Пример логики распознавания (вернет список найденных карт)
        # В будущем здесь подключается нарезка реальных зон
        detected = ["Менемен", "Торнадо Ужаса"]
        
        return {"status": "success", "cards": detected}
    except Exception as e:
        return {"status": "error", "message": str(e)}
