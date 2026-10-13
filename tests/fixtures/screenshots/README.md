# Screenshot regression fixtures

These fixtures test the full pipeline from an original inventory screenshot to detected card crops and quantities. They are distinct from `tests/fixtures/counter/`, which tests the counter detector on already-cropped card images.

## Files

- `cases.json` contains manually verified screenshot cases.
- `images/` stores original, unresized screenshots used by those cases.
- `tests/test_screenshot_fixtures.py` runs the scanner and checks count, crop quality, and quantity.

## Adding a case

1. Save the original screenshot as a PNG in `tests/fixtures/screenshots/images/`. Do not resize, compress, crop, or otherwise edit it.
2. Inspect the screenshot and manually label every relevant card rectangle as `[x, y, width, height]` in original screenshot coordinates.
3. Record the actual visible quantity. Use `1` when no duplicate counter is visible.
4. Use `state: "owned"` for cards that belong in inventory. Use `state: "unowned"` for darkened/locked cards that are visible but must not be counted in inventory.
5. Add the screenshot path, original dimensions, stable case ID, card IDs/names, rectangles, and verified quantities to `cases.json`.
6. Run `python -m pytest -q tests/test_screenshot_fixtures.py`. Fix the scanner if it fails; do not change manually verified boxes or quantities just to make the test pass.

Example:

```json
{
  "version": 1,
  "cases": [
    {
      "id": "inventory-gray-cards-01",
      "image_path": "tests/fixtures/screenshots/images/inventory-gray-cards-01.png",
      "source_screenshot_size": [1280, 720],
      "cards": [
        {
          "card_id": "verified-catalog-id",
          "card_name": "Manually verified card name",
          "box": [198, 147, 228, 337],
          "expected_quantity": 2,
          "state": "owned"
        },
        {
          "card_id": "another-verified-card-id",
          "card_name": "Dimmed card",
          "box": [450, 147, 228, 337],
          "expected_quantity": 1,
          "state": "unowned"
        }
      ]
    }
  ]
}
```

## What is checked

For each original screenshot, the tests verify:

- The number of detected inventory cards matches the number of labelled `owned` cards.
- Each detected rectangle overlaps its manually labelled rectangle with IoU >= 0.70.
- At least 90% of the labelled card area is retained and at least 75% of the crop belongs to the labelled card.
- The crop stays within the screenshot, has plausible portrait-card proportions, and does not clip the top or right edge where the quantity badge is located.
- The current scanner reads the manually verified quantity from each crop.
- Labelled `unowned`/dimmed cards are not returned as inventory crops.
- Fixture IDs, image paths, dimensions, rectangles, states, and quantities are valid.

The manifest currently contains no labelled screenshot cases, so the end-to-end screenshot test is skipped until at least one real, manually checked screenshot is added. The manifest validation test still runs. Do not claim full screenshot coverage until the fixture list contains representative screenshots, including different resolutions/layouts, multiple rows, gray/dimmed cards, and cards with and without quantity badges.
