import asyncio
import builtins
import io
import json
from pathlib import Path
import re

from PIL import Image
import pytest

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
        cards=[{"box": [10, 12, 40, 60], "expected_quantity": 2, "state": "owned", "card_id": "card_7", "identity_verified": True}],
    )

    result = main.save_screenshot_test(data, authorization=None)

    assert result["git_commit"] == "abc123"
    assert result["branch"] == "feature/card-counter"
    assert f"tests/fixtures/screenshots/images/{data.id}.png" in captured["files"]
    manifest = json.loads(captured["files"]["tests/fixtures/screenshots/cases.json"])
    assert manifest["cases"][0]["cards"][0]["expected_quantity"] == 2
    assert manifest["cases"][0]["cards"][0]["card_id"] == "card_7"
    assert manifest["cases"][0]["cards"][0]["identity_verified"] is True
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


def test_run_screenshot_fixtures_returns_full_report_and_summary_profile(monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "_resource_snapshot", lambda: {
        "rss_bytes": None, "vms_bytes": None,
        "user_cpu_seconds": None, "system_cpu_seconds": None,
        "container_memory_bytes": None, "container_cpu_seconds": None,
        "cpu_count": None, "available_cpu_count": None, "cpu_limit": None,
        "memory_limit_bytes": None, "metric_source": "stdlib",
        "collection_errors": [],
    })
    monkeypatch.setattr(main, "_screenshot_test_branch", lambda: "feature/card-counter")
    monkeypatch.setattr(
        main,
        "_screenshot_test_manifest",
        lambda branch: {
            "version": 1,
            "cases": [{
                "id": "inventory-test-profile",
                "image_path": "tests/fixtures/screenshots/images/inventory-test-profile.png",
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
            "profile": {"total_ms": 120.5, "detection_ms": 25.0, "counter_ms": 7.5},
        },
    )

    result = main.run_screenshot_tests(authorization=None)

    assert result["run_id"]
    assert result["profile"]["total_duration_ms"] >= 0
    assert result["profile"]["total_scan_time_ms"] >= 0
    assert result["results"][0]["profile"]["total_ms"] == 120.5
    assert result["results"][0]["timings"]["scanner_total_ms"] == 120.5
    assert result["report"]["schema_version"] == 2
    assert result["report"]["metadata"]["run_id"] == result["run_id"]
    assert result["report"]["metadata"]["case_count"] == 1
    assert result["report"]["cases"][0]["case_id"] == "inventory-test-profile"
    assert result["report"]["aggregate_timings"]["scan_ms"]["count"] == 1
    assert result["report"]["summary"]["median_scan_time_ms"] == pytest.approx(
        result["results"][0]["timings"]["scan_ms"]
    )
    assert re.fullmatch(
        r"scanner-quality-report-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.json",
        result["report_filename"],
    )
    serialized_report = json.loads(json.dumps(result["report"]))
    assert serialized_report["resource_usage"]["start_state"]["rss_bytes"] is None
    assert serialized_report["environment"]["memory_limit"] is None
    assert serialized_report["summary"]["peak_rss"] is None
    assert not hasattr(main, "SCREENSHOT_TEST_REPORTS")


def test_resource_monitor_stops_when_test_raises(monkeypatch):
    monitor = main._ResourceMonitor(interval_seconds=0.005)
    monkeypatch.setattr(main, "_resource_snapshot", lambda: {
        "rss_bytes": 100, "vms_bytes": 200, "user_cpu_seconds": 1.0,
        "system_cpu_seconds": 0.5, "container_memory_bytes": None,
        "container_cpu_seconds": None, "cpu_limit": None,
        "memory_limit_bytes": None, "collection_errors": [],
    })

    with pytest.raises(RuntimeError, match="test failure"):
        with monitor:
            raise RuntimeError("test failure")

    assert monitor.stop_event.is_set()
    assert monitor.thread is not None
    assert not monitor.thread.is_alive()
    assert monitor.summary()["sample_count"] >= 2


def test_resource_metrics_keep_process_and_container_separate():
    before = {
        "user_cpu_seconds": 1.0, "system_cpu_seconds": 0.5,
        "rss_bytes": 1000, "vms_bytes": 2000,
        "container_memory_bytes": 5000, "container_cpu_seconds": 2.0,
        "cpu_limit": 1.5, "memory_limit_bytes": 10000,
    }
    after = {
        "user_cpu_seconds": 1.2, "system_cpu_seconds": 0.6,
        "rss_bytes": 1200, "vms_bytes": 2200,
        "container_memory_bytes": 5500, "container_cpu_seconds": 2.25,
        "cpu_limit": 1.5, "memory_limit_bytes": 10000,
    }

    metrics = main._resource_delta(before, after, 0.5)

    assert metrics["user_cpu_delta"] == pytest.approx(0.2)
    assert metrics["system_cpu_delta"] == pytest.approx(0.1)
    assert metrics["cpu_avg_percent"] == pytest.approx(60)
    assert metrics["rss_delta"] == 200
    assert metrics["container_memory_after"] == 5500
    assert metrics["container_cpu_seconds_delta"] == pytest.approx(0.25)
    assert main._aggregate_resource_metric([10.0, 20.0]) == {
        "count": 2, "avg": 15.0, "median": 15.0, "min": 10.0,
        "max": 20.0, "p90": 20.0,
    }


def test_resource_snapshot_does_not_require_psutil(monkeypatch):
    original_import = builtins.__import__

    def reject_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise AssertionError("psutil must remain optional")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_psutil)

    snapshot = main._resource_snapshot()

    assert snapshot["metric_source"] == "stdlib"
    assert "collection_errors" in snapshot


def test_run_screenshot_tests_retains_admin_authorization(monkeypatch):
    def reject_admin(authorization):
        raise main.HTTPException(status_code=401, detail="Unauthorized")

    monkeypatch.setattr(main, "require_admin", reject_admin)

    with pytest.raises(main.HTTPException) as error:
        main.run_screenshot_tests(authorization=None)

    assert error.value.status_code == 401


def test_quality_report_download_uses_authorized_run_response():
    admin_html = Path(main.__file__).with_name("admin.html").read_text(encoding="utf-8")

    assert "api('/admin/screenshot-tests/run'" in admin_html
    assert "JSON.stringify(d.report,null,2)" in admin_html
    assert "report_download_url" not in admin_html
    assert "link.download=d.report_filename" in admin_html


def test_screenshot_run_keeps_processing_after_a_case_scan_error(monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "_screenshot_test_branch", lambda: "feature/card-counter")
    monkeypatch.setattr(main, "load_db", lambda: {})
    monkeypatch.setattr(
        main,
        "_screenshot_test_manifest",
        lambda branch: {
            "version": 1,
            "cases": [
                {
                    "id": "broken-case",
                    "image_path": "tests/fixtures/screenshots/images/broken-case.png",
                    "cards": [],
                },
                {
                    "id": "valid-case",
                    "image_path": "tests/fixtures/screenshots/images/valid-case.png",
                    "cards": [{"box": [10, 12, 40, 60], "expected_quantity": 2, "state": "owned"}],
                },
            ],
        },
    )
    monkeypatch.setattr(main, "_github_read_bytes", lambda path, branch: _png_bytes())
    scan_calls = 0

    def fail_first_scan(image, save_previews=False):
        nonlocal scan_calls
        scan_calls += 1
        if scan_calls == 1:
            raise ValueError("private details are not returned")
        return {"cards": [{"box": [10, 12, 40, 60], "quantity": 2}], "profile": {}}

    monkeypatch.setattr(main, "scan_screenshot", fail_first_scan)
    monkeypatch.setattr(main, "_enrich_admin_scan", lambda cards, catalog: cards)

    result = main.run_screenshot_tests(authorization=None)

    assert result["count"] == 2
    assert result["errors"] == 1
    assert [case["status"] for case in result["report"]["cases"]] == ["error", "passed"]
    assert result["report"]["cases"][0]["error_stage"] == "scan"
    assert "private details" not in result["report"]["cases"][0]["error"]


def test_update_screenshot_fixture_identity_commits_manual_labels(monkeypatch):
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
                "cards": [
                    {"box": [10, 12, 40, 60], "state": "owned", "card_id": "card_7"},
                    {"box": [50, 12, 40, 60], "state": "owned", "card_id": ""},
                ],
            }],
        },
    )
    monkeypatch.setattr(main, "load_db", lambda: {"card_7": {"name": "Проверенная карта"}})
    captured = {}
    monkeypatch.setattr(
        main,
        "_github_commit_files",
        lambda files, message, branch: captured.update(files=files, message=message, branch=branch) or {"sha": "abc123"},
    )
    result = main.update_screenshot_test_identity(
        "inventory-test-01",
        main.ScreenshotFixtureIdentityUpdateSchema(cards=[
            {"card_id": "card_7", "identity_verified": True},
            {"card_id": "", "identity_verified": True},
        ]),
        authorization=None,
    )
    manifest = json.loads(captured["files"]["tests/fixtures/screenshots/cases.json"])
    cards = manifest["cases"][0]["cards"]
    assert result["verified_cards"] == 2
    assert result["git_commit"] == "abc123"
    assert cards[0]["card_id"] == "card_7"
    assert cards[0]["card_name"] == "Проверенная карта"
    assert cards[0]["identity_verified"] is True
    assert cards[1]["card_id"] == ""
    assert cards[1]["identity_verified"] is True


def test_run_screenshot_fixtures_reports_wrong_catalog_identity(monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "_screenshot_test_branch", lambda: "feature/card-counter")
    monkeypatch.setattr(
        main,
        "_screenshot_test_manifest",
        lambda branch: {
            "version": 1,
            "cases": [{
                "id": "identity-test-01",
                "image_path": "tests/fixtures/screenshots/images/identity-test-01.png",
                "cards": [{
                    "box": [10, 12, 40, 60], "expected_quantity": 2,
                    "state": "owned", "card_id": "card_7", "identity_verified": True,
                }],
            }],
        },
    )
    monkeypatch.setattr(main, "_github_read_bytes", lambda path, branch: _png_bytes())
    monkeypatch.setattr(
        main,
        "scan_screenshot",
        lambda image, save_previews=False: {
            "count": 1, "cards": [{"box": [10, 12, 40, 60], "quantity": 2, "hashes": {}}],
        },
    )
    monkeypatch.setattr(main, "load_db", lambda: {"card_7": {"name": "Ожидаемая"}, "card_9": {"name": "Ошибочная"}})
    monkeypatch.setattr(
        main,
        "_enrich_admin_scan",
        lambda cards, db: [{**cards[0], "matched": True, "id": "card_9"}],
    )
    result = main.run_screenshot_tests(authorization=None)
    assert result["failed"] == 1
    assert result["results"][0]["identity_checked"] == 1
    assert result["results"][0]["identity_matches"] == 0
    detail = result["results"][0]["identity_details"][0]
    assert detail["decision"] == "wrong_id"
    assert detail["expected_card_id"] == "card_7"
    assert detail["matched_card_id"] == "card_9"
    assert "identity_stats" in result["report"]["summary"]
    assert result["report"]["summary"]["identity_stats"]["wrong"] == 1
    assert result["report"]["cases"][0]["identity_details"][0]["decision"] == "wrong_id"
    assert "identity_decision_counts" in result["report"]["cases"][0]
    assert "identity_matches" in result["report"]["cases"][0]["matches"]
    assert any("ID карты" in error and "card_7" in error and "card_9" in error for error in result["results"][0]["errors"])


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
