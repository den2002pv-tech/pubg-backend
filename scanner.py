import numpy as np
from PIL import Image

def scan_screenshot(pil_img: Image.Image):
    """
    Стабильная нарезка скриншота инвентаря PUBG Mobile на 8 карт (сетка 2x4).
    Вырезает чистую зону с иллюстрацией для идеального хэширования.
    """
    w, h = pil_img.size
    aspect_ratio = w / float(h)

    # Если это широкий полноэкранный скриншот инвентаря
    if aspect_ratio > 1.35:
        # Относительные координаты сетки всего блока карт на экране (в долях от 0 до 1)
        grid_left = w * 0.135
        grid_top = h * 0.125
        grid_right = w * 0.725
        grid_bottom = h * 0.840

        grid_w = grid_right - grid_left
        grid_h = grid_bottom - grid_top

        cols = 4
        rows = 2

        cell_w = grid_w / cols
        cell_h = grid_h / rows

        cards = []
        for r in range(rows):
            for c in range(cols):
                # Базовые координаты ячейки карты
                x1 = grid_left + c * cell_w
                y1 = grid_top + r * cell_h
                x2 = x1 + cell_w
                y2 = y1 + cell_h

                # ВНИМАНИЕ: Делаем внутренний Crop (сужаем область внутрь карты),
                # чтобы отсечь внешние рамки, счетчики (x3) и нижнюю плашку с текстом.
                # Оставляем только чистый арт/иллюстрацию по центру.
                pad_x = cell_w * 0.12
                pad_top = cell_h * 0.18      # отсекаем верхний край рамки и счетчик
                pad_bottom = cell_h * 0.32   # отсекаем нижнюю плашку с названием

                art_x1 = int(x1 + pad_x)
                art_y1 = int(y1 + pad_top)
                art_x2 = int(x2 - pad_x)
                art_y2 = int(y2 - pad_bottom)

                card_crop = pil_img.crop((art_x1, art_y1, art_x2, art_y2))
                cards.append({'crop': card_crop})

        return cards

    # Если загружена одна отдельно вырезанная карта
    return [{'crop': pil_img}]
