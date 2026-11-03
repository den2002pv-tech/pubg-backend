import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request
import pytest

import main
from scanner_diagnostics import ScannerDiagnosticStore
from scanner_prewarm import ScannerPrewarmController


def _store(tmp_path, **options):
    return ScannerDiagnosticStore(directory=tmp_path / "scanner-diagnostics", **options)


def test_jsonl_records_are_valid_and_exclude_sensitive_fields(tmp_path):
    store = _store(tmp_path)
    assert store.record_event({
        "event": "scan_completed",
        "session_id": str(uuid.uuid4()),
        "image_width": 120,
        "image_format": "PNG",
        "authorization": "Bearer not-for-logs",
        "telegram_id": 123456,
        "nested": {"database_url": "postgres://not-for-logs"},
    })

    path = next(store.directory.glob("scanner-events-*.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["image_width"] == 120
    assert rows[0]["image_format"] == "PNG"
    assert "authorization" not in rows[0]
    assert "telegram_id" not in rows[0]
    assert "database_url" not in rows[0]["nested"]
    assert path.stat().st_mode & 0o077 == 0


def test_concurrent_jsonl_writes_do_not_lose_or_corrupt_records(tmp_path):
    store = _store(tmp_path)

    def write_batch(batch):
        for item in range(40):
            assert store.record_event({"event": "parallel", "batch": batch, "item": item})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_batch, range(8)))

    path = next(store.directory.glob("scanner-events-*.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 320
    assert len({(row["batch"], row["item"]) for row in rows}) == 320


def test_rotation_total_limit_and_unrelated_files_are_preserved(tmp_path):
    directory = tmp_path / "scanner-diagnostics"
    store = ScannerDiagnosticStore(
        directory=directory,
        max_file_bytes=250,
        max_total_bytes=5000,
    )
    unrelated = directory / "other-subsystem.jsonl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep me", encoding="utf-8")

    for item in range(20):
        assert store.record_event({"event": "rotation", "item": item, "padding": "x" * 70})

    files = list(directory.glob("scanner-events-*.jsonl"))
    assert len(files) > 1
    assert all(path.stat().st_size <= 250 for path in files)
    assert sum(path.stat().st_size for path in files) <= 5000
    assert unrelated.read_text(encoding="utf-8") == "keep me"

    bounded = ScannerDiagnosticStore(
        directory=tmp_path / "bounded-diagnostics",
        max_file_bytes=250,
        max_total_bytes=500,
    )
    for item in range(20):
        assert bounded.record_event({"event": "bounded", "item": item, "padding": "x" * 70})
    assert sum(path.stat().st_size for path in bounded.directory.glob("scanner-events-*.jsonl")) <= 500


def test_retention_removes_only_old_diagnostic_files(tmp_path):
    store = _store(tmp_path, retention_days=1)
    store.record_event({"event": "old"})
    old_file = next(store.directory.glob("scanner-events-*.jsonl"))
    os.utime(old_file, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    unrelated = store.directory / "keep.jsonl"
    unrelated.write_text("not ours", encoding="utf-8")

    store.record_event({"event": "new"})

    remaining = store.read_records()
    assert [row["event"] for row in remaining] == ["new"]
    assert len(list(store.directory.glob("scanner-events-*.jsonl"))) == 1
    assert unrelated.read_text(encoding="utf-8") == "not ours"


def test_filesystem_failure_does_not_raise(tmp_path, monkeypatch, caplog):
    store = _store(tmp_path)
    monkeypatch.setattr("scanner_diagnostics.os.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")))

    assert store.record_event({"event": "scan_completed"}) is False
    assert "Scanner diagnostics write failed" in caplog.text


def test_safe_file_rejects_unknown_names_and_path_traversal(tmp_path):
    store = _store(tmp_path)
    store.record_event({"event": "downloadable"})
    path = next(store.directory.glob("scanner-events-*.jsonl"))

    assert store.safe_file(path.name) == path.resolve()
    assert store.safe_file("../scanner-events-20261031-1-0001.jsonl") is None
    assert store.safe_file("other.jsonl") is None


def test_summary_separates_first_and_subsequent_scans(tmp_path):
    store = _store(tmp_path)
    session_id = str(uuid.uuid4())
    store.record_event({"event": "worker_start"})
    store.record_event({"event": "warmup_completed", "warmup_duration_ms": 12.5})
    for index, duration in enumerate((100.0, 40.0)):
        event = store.record_scan(session_id, {
            "event": "scan_completed",
            "http_status": 200,
            "total_server_ms": duration,
            "warmup_status_at_scan": "completed",
        })
        store.record_event(event)

    summary = store.summary()
    assert summary["scan_count"] == 2
    assert summary["first_and_subsequent"]["first_scans"]["median_ms"] == 100
    assert summary["first_and_subsequent"]["subsequent_scans"]["median_ms"] == 40
    assert summary["process_start_count"] == 1
    assert summary["warmup_duration"]["avg_ms"] == 12.5
    assert summary["cpu_experiment"]["enabled"] is False


@pytest.mark.parametrize("target_seconds", (5, 10, 30, 60))
def test_scan_intervals_are_bucketed_only_near_measured_targets(
    tmp_path, monkeypatch, target_seconds
):
    store = _store(tmp_path)
    session_id = str(uuid.uuid4())
    clock = iter((100.0, 100.0 + target_seconds + 0.5))
    monkeypatch.setattr("scanner_diagnostics.time.monotonic", lambda: next(clock))

    store.record_scan(session_id, {"event": "scan_completed"})
    event = store.record_scan(session_id, {"event": "scan_completed"})

    assert event["scan_interval_seconds"] == target_seconds + 0.5
    assert event["scan_interval_target_seconds"] == target_seconds


def test_disabled_diagnostics_do_not_write_or_prevent_normal_results(tmp_path):
    store = _store(tmp_path, enabled=False)
    assert store.record_event({"event": "scan_completed"}) is False
    assert store.list_files() == []
    assert store.summary()["scan_count"] == 0


def test_authenticated_routes_require_admin_authorization(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    for operation in (
        main.list_scanner_diagnostic_files,
        main.scanner_diagnostics_summary,
        main.download_scanner_diagnostic_file,
    ):
        with pytest.raises(HTTPException) as error:
            if operation is main.download_scanner_diagnostic_file:
                asyncio.run(operation("scanner-events-20261031-123-0001.jsonl", None))
            else:
                asyncio.run(operation(None))
        assert error.value.status_code == 503


def test_admin_download_returns_existing_file_and_rejects_unknown(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.record_event({"event": "downloadable"})
    path = next(store.directory.glob("scanner-events-*.jsonl"))
    monkeypatch.setattr(main, "SCANNER_DIAGNOSTICS", store)
    monkeypatch.setattr(main, "require_admin", lambda _authorization: None)

    result = asyncio.run(
        main.download_scanner_diagnostic_file(path.name, authorization=None)
    )
    assert result.path == path.resolve()
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            main.download_scanner_diagnostic_file("../secret.jsonl", authorization=None)
        )
    assert error.value.status_code == 404


def test_scan_middleware_records_status_pid_and_first_scan(tmp_path, monkeypatch):
    store = _store(tmp_path)
    controller = ScannerPrewarmController(store, lambda: None)
    monkeypatch.setattr(main, "SCANNER_DIAGNOSTICS", store)
    monkeypatch.setattr(main, "SCANNER_PREWARM", controller)
    session_id = str(uuid.uuid4())

    async def exercise():
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/scan",
            "query_string": b"",
            "headers": [(b"x-scanner-session-id", session_id.encode("ascii"))],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "http_version": "1.1",
        }
        request = Request(scope)

        async def call_next(current):
            current.state.scanner_diagnostics["request_body_read_ms"] = 1.25
            current.state.scanner_diagnostics["queue_wait_ms"] = 2.5
            current.state.scanner_diagnostics["detection_ms"] = 4.0
            return Response(status_code=200)

        return await main.scanner_diagnostics_middleware(request, call_next)

    assert asyncio.run(exercise()).status_code == 200
    records = store.read_records()
    scan = next(row for row in records if row.get("event") == "scan_completed")
    assert scan["worker_pid"] == os.getpid()
    assert scan["first_scan_classifications"] == ["first_after_process_start"]
    assert scan["request_body_read_ms"] == 1.25
    assert scan["queue_wait_ms"] == 2.5
    assert scan["detection_ms"] == 4.0
    assert scan["http_status"] == 200


def test_prewarm_is_deduplicated_and_skips_active_requests(tmp_path):
    calls = []
    controller = ScannerPrewarmController(_store(tmp_path), lambda: calls.append("warmup"))
    session = str(uuid.uuid4())

    async def exercise():
        skipped = await controller.on_trigger("scanner_open", session, active_requests=1)
        assert skipped["reason"] == "active_user_request"
        started = await controller.on_trigger("scanner_open", session, active_requests=0)
        assert started["warmup_status"] == "running"
        duplicate = await controller.on_trigger("scanner_open", session, active_requests=0)
        assert duplicate["reason"] == "duplicate_trigger"
        await asyncio.sleep(0.05)

    asyncio.run(exercise())
    assert calls == ["warmup"]
    assert controller.status == "completed"


def test_prewarm_flags_and_failure_are_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_PREWARM_ENABLED", "true")
    monkeypatch.setenv("SCANNER_PREWARM_ON_OPEN_ENABLED", "false")
    disabled = ScannerPrewarmController(_store(tmp_path / "disabled"), lambda: None)
    disabled_result = asyncio.run(
        disabled.on_trigger("scanner_open", str(uuid.uuid4()), active_requests=0)
    )
    assert disabled_result["reason"] == "trigger_disabled"
    assert disabled.status == "not_started"

    def fail():
        raise RuntimeError("error contains no user data")

    failed = ScannerPrewarmController(_store(tmp_path / "failed"), fail)
    monkeypatch.setenv("SCANNER_PREWARM_ON_OPEN_ENABLED", "true")
    session = str(uuid.uuid4())

    async def exercise():
        await failed.on_trigger("scanner_open", session, active_requests=0)
        await asyncio.sleep(0.05)

    asyncio.run(exercise())
    assert failed.status == "failed"
    assert any(row.get("event") == "warmup_failed" for row in failed.diagnostics.read_records())
    assert all(
        "error contains no user data" not in path.read_text(encoding="utf-8")
        for path in failed.diagnostics.directory.glob("*.jsonl")
    )


def test_invalid_cpu_experiment_flag_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_PREWARM_CPU_EXPERIMENT_ENABLED", "sometimes")
    controller = ScannerPrewarmController(_store(tmp_path), lambda: None)

    with pytest.raises(ValueError, match="must be a boolean"):
        controller.snapshot()


def test_user_online_transition_is_process_local_and_private(tmp_path):
    store = _store(tmp_path)
    session_id = str(uuid.uuid4())
    assert store.note_user_online(123456)
    assert not store.note_user_online(123456)
    store.record_trigger(session_id, "user_online")
    assert all(
        "123456" not in path.read_text(encoding="utf-8")
        for path in store.directory.glob("*.jsonl")
    )
