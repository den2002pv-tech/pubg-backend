import asyncio
import io
import json
from pathlib import Path

from PIL import Image

import main


def _png_bytes(size=(100, 120)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_screenshot_fixture_preview_keeps_original_png_and_returns_detected_boxes(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(
        main,
        "scan_screenshot",
        lambda image, save_previews=False: {
            "count": 1,
            "cards": [{"box": [10, 12, 40, 60], "quantity": 3}],
        },
    )
    monkeypatch.setattr(
        main,
        "_enrich_admin_scan",
        lambda cards, db: [{
            **cards[0], "matched": True, "id": "card_7",
            "name": "Проверенная карта", "icon": "/static/cards/card_7.webp",
            "distance": 2.5,
        }],
    )
    upload = main.UploadFile(filename="inventory.png", file=io.BytesIO(_png_bytes()))

    result = asyncio.run(main.preview_screenshot_test(file=upload, authorization=None))

    saved = tmp_path / f"screenshot-fixture-{result['upload_id']}.png"
    assert saved.read_bytes() == _png_bytes()
    assert result["image_size"] == [100, 120]
    assert result["cards"] == [{
        "box": [10, 12, 40, 60], "quantity": 3, "matched": True,
        "card_id": "card_7", "card_name": "Проверенная карта",
        "icon": "/static/cards/card_7.webp", "match_distance": 2.5,
    }]
    assert result["image_url"].endswith(result["upload_id"])


def test_save_screenshot_fixture_commits_image_and_verified_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(main, "_screenshot_test_branch", lambda: "feature/card-counter")
    monkeypatch.setattr(main, "_screenshot_test_manifest", lambda branch: {"version": 1, "cases": []})
    captured = {}
    monkeypatch.setattr(
        main,
        "_github_commit_files",
        lambda files, message, branch: captured.update(files=files, message=message, branch=branch) or {"sha": "abc123"},
    )
    upload_id = "a" * 32
    (tmp_path / f"screenshot-fixture-{upload_id}.png").write_bytes(_png_bytes())
    data = main.ScreenshotFixtureSaveSchema(
        upload_id=upload_id,
        id="inventory-test-01",
        title="Checked screenshot",
        cards=[{"box": [10, 12, 40, 60], "expected_quantity": 2, "state": "owned"}],
    )

    result = main.save_screenshot_test(data, authorization=None)

    assert result["git_commit"] == "abc123"
    assert result["branch"] == "feature/card-counter"
    assert f"tests/fixtures/screenshots/images/{data.id}.png" in captured["files"]
    manifest = json.loads(captured["files"]["tests/fixtures/screenshots/cases.json"])
    assert manifest["cases"][0]["cards"][0]["expected_quantity"] == 2
    assert manifest["cases"][0]["source_screenshot_size"] == [100, 120]
    assert not (tmp_path / f"screenshot-fixture-{upload_id}.png").exists()


def test_run_screenshot_fixtures_checks_card_count_boxes_and_quantity(monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "_screenshot_test_branch", lambda: "feature/card-counter")
    monkeypatch.setattr(
        main,
        "_screenshot_test_manifest",
        lambda branch: {
            "version": 1,
            "cases": [{
                "id": "inventory-test-01",
                "image_path": "tests/fixtures/screenshots/images/inventory-test-01.png",
                "cards": [{"box": [10, 12, 40, 60], "expected_quantity": 2, "state": "owned"}],
            }],
        },
    )
    monkeypatch.setattr(main, "_github_read_bytes", lambda path, branch: _png_bytes())
    monkeypatch.setattr(
        main,
        "scan_screenshot",
        lambda image, save_previews=False: {
            "count": 1,
            "cards": [{"box": [11, 13, 39, 58], "quantity": 2}],
        },
    )

    result = main.run_screenshot_tests(authorization=None)

    assert result["count"] == 1
    assert result["passed"] == 1
    assert result["failed"] == 0
    assert result["results"][0]["passed"] is True


def test_github_commit_files_includes_required_tree_fields_for_deletions(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    calls = []

    def fake_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if url.endswith("/git/ref/heads/feature%2Fcard-counter"):
            return {"object": {"sha": "parent-sha"}}
        if url.endswith("/git/commits/parent-sha"):
            return {"tree": {"sha": "base-tree-sha"}}
        if url.endswith("/git/trees"):
            return {"sha": "new-tree-sha"}
        if url.endswith("/git/commits"):
            return {"sha": "new-commit-sha", "html_url": "https://example.test/commit"}
        if url.endswith("/git/refs/heads/feature%2Fcard-counter"):
            return {}
        raise AssertionError(f"Unexpected GitHub request: {method} {url}")

    monkeypatch.setattr(main, "_github_request", fake_request)

    result = main._github_commit_files(
        {"tests/fixtures/screenshots/images/old.png": None},
        "Delete screenshot fixture",
        "feature/card-counter",
    )

    tree_call = next(call for call in calls if call[1].endswith("/git/trees"))
    entry = tree_call[2]["tree"][0]
    assert entry == {
        "path": "tests/fixtures/screenshots/images/old.png",
        "mode": "100644",
        "type": "blob",
        "sha": None,
    }
    assert result["sha"] == "new-commit-sha"

