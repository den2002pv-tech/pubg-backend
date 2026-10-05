import cv2
import numpy as np
from PIL import Image

def scan_screenshot(pil_img: Image.Image):
    """
    Автоматически находит контуры карточек на скриншоте с помощью OpenCV.
    Возвращает список словарей: [{'crop': PIL_Image, 'hash': str}, ...]
    """
    # Переводим PIL Image в формат OpenCV (BGR)
    open_cv_image = np.array(pil_img)
    open_cv_image = cv2.cvtColor(open_cv_image, cv2.COLOR_RGB2BGR)
    
    gray = cv2.cvtColor(open_cv_image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.threshold(blurred, 60, 255, cv2.THRESH_BINARY)[1]

    # Ищем контуры
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    detected_cards = []
    h_img, w_img, _ = open_cv_image.shape
    min_area = (w_img * h_img) * 0.02  # Минимальный размер карточки
    
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area > min_area:
            x, y, w, h = cv2.boundingRect(cnt)
            
            # Фильтруем по соотношению сторон (карточки обычно вытянуты по вертикали)
            aspect_ratio = h / float(w)
            if 1.1 < aspect_ratio < 1.8:
                crop = open_cv_image[y:y+h, x:x+w]
                crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                pil_crop = Image.fromarray(crop_rgb)
                
                detected_cards.append({
                    'crop': pil_crop,
                    'hash': '' # Хэш посчитаем в main.py
                })
                
    # Сортируем найденные карты сверху вниз, слева направо
    detected_cards.sort(key=lambda item: (item['crop'].size[1], item['crop'].size[0]))
    return detected_cards
  
