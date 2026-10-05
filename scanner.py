import numpy as np
from PIL import Image

def scan_screenshot(pil_img: Image.Image):
    """
    Сканирует скриншот инвентаря PUBG Mobile:
    - Если загружен скриншот инвентаря (широкий экран), нарезает сетку 2x4 на 8 отдельных карт.
    - Если загружена 1 отдельная карта, возвращает её как есть.
    """
    w, h = pil_img.size
    aspect_ratio = w / float(h)

    # Если это полноэкранный скриншот (соотношение сторон > 1.35)
    if aspect_ratio > 1.35:
        # Границы сетки инвентаря PUBG Mobile (в процентах от размера экрана)
        grid_left = int(w * 0.205)
        grid_top = int(h * 0.225)
        grid_right = int(w * 0.725)
        grid_bottom = int(h * 0.840)

        grid_w = grid_right - grid_left
        grid_h = grid_bottom - grid_top

        cols = 4
        rows = 2

        cell_w = grid_w / cols
        cell_h = grid_h / rows

        cards = []
        for r in range(rows):
            for c in range(cols):
                x1 = int(grid_left + c * cell_w)
                y1 = int(grid_top + r * cell_h)
                x2 = int(x1 + cell_w)
                y2 = int(y1 + cell_h)

                card_crop = pil_img.crop((x1, y1, x2, y2))
                cards.append({'crop': card_crop})

        return cards

    # Если загружена одна уже вырезанная карта
    return [{'crop': pil_img}]
