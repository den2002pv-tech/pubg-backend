import sys
import time

sys.path.insert(0, "/workspace")


def main() -> None:
    import main

    # 1) warmup runs without error
    t0 = time.perf_counter()
    main._scanner_warmup()
    warmup_ms = (time.perf_counter() - t0) * 1000
    print(f"warmup: {warmup_ms:.0f} ms")

    # 2) synthetic warmup image detects cards (so JPEG preview path is exercised)
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (1280, 720), (22, 28, 34))
    draw = ImageDraw.Draw(image)
    for index, x in enumerate((200, 402, 604, 806)):
        draw.rectangle((x, 150, x + 179, 419),
                       fill=(45 + index * 18, 72 + index * 12, 105 + index * 10),
                       outline=(235, 235, 235), width=4)
        draw.rectangle((x + 12, 170, x + 167, 300),
                       fill=(60 + index * 20, 90 + index * 10, 120 + index * 8))
        draw.line((x + 15, 340, x + 164, 340), fill=(210, 170, 75), width=3)
    result = main.scan_screenshot(image, save_previews=True)
    n_cards = len(result.get("cards", []))
    has_preview = bool(result["cards"] and result["cards"][0].get("preview"))
    print(f"synthetic scan: cards={n_cards} preview_jpeg={has_preview}")

    # 3) first REAL png scan after warmup: decode should be fast now
    from pathlib import Path
    png = sorted(Path("/workspace/tests/fixtures/screenshots/images").glob("*.png"))[0]
    raw = png.read_bytes()
    t0 = time.perf_counter()
    r1 = main.scan_screenshot(raw, save_previews=True)
    first_ms = (time.perf_counter() - t0) * 1000
    p1 = r1["profile"]
    t0 = time.perf_counter()
    r2 = main.scan_screenshot(raw, save_previews=True)
    second_ms = (time.perf_counter() - t0) * 1000
    p2 = r2["profile"]
    print(f"real png first scan:  {first_ms:.0f} ms (decode={p1.get('decode_ms', 0):.0f})")
    print(f"real png second scan: {second_ms:.0f} ms (decode={p2.get('decode_ms', 0):.0f})")
    print(f"first-vs-second delta: {first_ms - second_ms:+.0f} ms")


if __name__ == "__main__":
    main()
