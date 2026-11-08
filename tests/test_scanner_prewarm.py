import asyncio
import threading
import time

import pytest

import scanner_prewarm
from scanner_prewarm import ScannerPrewarmController


class FakeDiagnostics:
    def __init__(self):
        self.started_monotonic = time.perf_counter()
        self.events = []
        self.triggers = []

    def resource_metrics(self, active_requests=0):
        return {"available_memory_mb": 512}

    def record_event(self, event):
        self.events.append(dict(event))

    def record_trigger(self, session_id, trigger, **kwargs):
        self.triggers.append((session_id, trigger, kwargs))


def test_warmup_retries_after_exception():
    diagnostics = FakeDiagnostics()
    attempts = 0

    def warmup():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")

    async def exercise():
        controller = ScannerPrewarmController(diagnostics, warmup)
        first = await controller.on_application_start()
        assert first["warmup_status"] == "failed"
        second = await controller.on_application_start()
        assert second["warmup_status"] == "completed"
        assert attempts == 2
        await controller.shutdown()

    asyncio.run(exercise())


def test_timeout_does_not_allow_concurrent_retry_and_retries_after_worker_finishes(monkeypatch):
    monkeypatch.setattr(scanner_prewarm, "_WARMUP_TIMEOUT_SECONDS", 0.01)
    diagnostics = FakeDiagnostics()
    started = threading.Event()
    release = threading.Event()
    attempts = 0

    def warmup():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            release.wait(timeout=2)

    async def exercise():
        controller = ScannerPrewarmController(diagnostics, warmup)
        first = await controller.on_application_start()
        assert first["warmup_status"] == "failed"
        assert started.is_set()

        blocked = await controller.on_application_start()
        assert blocked["reason"] == "warmup_worker_still_running"
        assert attempts == 1

        release.set()
        worker = controller._worker_task
        assert worker is not None
        await asyncio.wait_for(asyncio.shield(worker), timeout=1)

        retried = await controller.on_application_start()
        assert retried["warmup_status"] == "completed"
        assert attempts == 2
        await controller.shutdown()

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_cancelled_warmup_is_reported_and_can_be_retried():
    diagnostics = FakeDiagnostics()
    started = threading.Event()
    release = threading.Event()
    attempts = 0

    def warmup():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            release.wait(timeout=2)

    async def exercise():
        controller = ScannerPrewarmController(diagnostics, warmup)
        await controller.on_trigger("scanner_open", "session-1", 0)
        task = controller._task
        assert task is not None
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert controller.status == "cancelled"
        release.set()
        worker = controller._worker_task
        assert worker is not None
        await asyncio.wait_for(asyncio.shield(worker), timeout=1)

        result = await controller.on_application_start()
        assert result["warmup_status"] == "completed"
        assert attempts == 2
        await controller.shutdown()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
