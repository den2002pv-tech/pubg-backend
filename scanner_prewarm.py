from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, Callable

from scanner_diagnostics import ScannerDiagnosticStore


_TRIGGER_FLAGS = {
    "user_online": "SCANNER_PREWARM_ONLINE_ENABLED",
    "scanner_open": "SCANNER_PREWARM_ON_OPEN_ENABLED",
    "image_selected": "SCANNER_PREWARM_ON_FILE_SELECT_ENABLED",
    "before_scan": "SCANNER_PREWARM_ON_SCAN_ENABLED",
}
_WARMUP_TIMEOUT_SECONDS = 2.0
_TRIGGER_COOLDOWN_SECONDS = 30.0


def _enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _cpu_experiment_requested() -> bool:
    return _enabled("SCANNER_PREWARM_CPU_EXPERIMENT_ENABLED", False)


class ScannerPrewarmController:
    def __init__(
        self,
        diagnostics: ScannerDiagnosticStore,
        warmup: Callable[[], None],
    ) -> None:
        self.diagnostics = diagnostics
        self._warmup = warmup
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._attempted = False
        self._status = "not_started"
        self._duration_ms: float | None = None
        self._completed_at: float | None = None
        self._completed_monotonic: float | None = None
        self._last_trigger_at: dict[tuple[str, str], float] = {}
        self._trigger: str | None = None

    @property
    def status(self) -> str:
        return self._status

    @property
    def trigger(self) -> str | None:
        return self._trigger

    @property
    def duration_ms(self) -> float | None:
        return self._duration_ms

    def snapshot(self) -> dict[str, Any]:
        return {
            "warmup_status": self._status,
            "warmup_trigger": self._trigger,
            "warmup_duration_ms": self._duration_ms,
            "warmup_completed_at": self._completed_at,
            "prewarm_mode": "lightweight",
            "cpu_experiment_enabled": False,
            "cpu_experiment_requested": _cpu_experiment_requested(),
        }

    def warmup_age_ms(self, now: float | None = None) -> float | None:
        if self._completed_monotonic is None:
            return None
        current = time.perf_counter() if now is None else now
        if current < self._completed_monotonic:
            return None
        return round((current - self._completed_monotonic) * 1000, 3)

    @staticmethod
    def feature_flags() -> dict[str, bool]:
        return {
            "enabled": _enabled("SCANNER_PREWARM_ENABLED", True),
            "on_start": _enabled("SCANNER_PREWARM_ON_START_ENABLED", True),
            "user_online": _enabled("SCANNER_PREWARM_ONLINE_ENABLED", True),
            "scanner_open": _enabled("SCANNER_PREWARM_ON_OPEN_ENABLED", True),
            "image_selected": _enabled("SCANNER_PREWARM_ON_FILE_SELECT_ENABLED", True),
            "before_scan": _enabled("SCANNER_PREWARM_ON_SCAN_ENABLED", True),
            "cpu_experiment_enabled": False,
            "cpu_experiment_requested": _cpu_experiment_requested(),
        }

    async def shutdown(self) -> None:
        # A timed-out asyncio wrapper does not stop its underlying worker thread.
        # Keep shutdown bounded so a stuck warmup cannot hang application shutdown.
        for task in (self._task, self._worker_task):
            if task is None or task.done():
                continue
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=_WARMUP_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise

    async def on_application_start(self, active_requests: int = 0) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        if not _enabled("SCANNER_PREWARM_ENABLED", True) or not _enabled(
            "SCANNER_PREWARM_ON_START_ENABLED", True
        ):
            return self._report(
                session_id,
                "application_start",
                "skipped",
                "startup_prewarm_disabled",
                active_requests,
            )
        return await self._start(
            "application_start", session_id, active_requests, wait=True
        )

    async def on_trigger(
        self,
        trigger: str,
        session_id: str,
        active_requests: int,
    ) -> dict[str, Any]:
        if trigger not in _TRIGGER_FLAGS:
            return self._report(
                session_id,
                trigger,
                "skipped",
                "unsupported_trigger",
                active_requests,
            )

        if not _enabled("SCANNER_PREWARM_ENABLED", True):
            return self._report(
                session_id, trigger, "skipped", "prewarm_disabled", active_requests
            )
        if not _enabled(_TRIGGER_FLAGS[trigger], True):
            return self._report(
                session_id, trigger, "skipped", "trigger_disabled", active_requests
            )
        if trigger == "before_scan":
            return {**self.snapshot(), "reason": "real_scan_has_priority"}

        now = time.monotonic()
        key = (session_id, trigger)
        previous = self._last_trigger_at.get(key)
        self._last_trigger_at[key] = now
        for old_key, old_at in tuple(self._last_trigger_at.items()):
            if now - old_at > 3600:
                self._last_trigger_at.pop(old_key, None)
        if len(self._last_trigger_at) > 10_000:
            oldest = sorted(self._last_trigger_at.items(), key=lambda item: item[1])
            for old_key, _ in oldest[: len(oldest) - 8_000]:
                self._last_trigger_at.pop(old_key, None)
        if previous is not None and now - previous < _TRIGGER_COOLDOWN_SECONDS:
            return self._report(
                session_id, trigger, self._status, "duplicate_trigger", active_requests
            )
        return await self._start(trigger, session_id, active_requests, wait=False)

    async def _start(
        self,
        trigger: str,
        session_id: str,
        active_requests: int,
        *,
        wait: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._status == "completed":
                return self._report(
                    session_id, trigger, "completed", "already_warm", active_requests
                )
            if self._task is not None and not self._task.done():
                return self._report(
                    session_id, trigger, "running", "warmup_in_progress", active_requests
                )
            if self._worker_task is not None:
                if not self._worker_task.done():
                    return self._report(
                        session_id, trigger, self._status,
                        "warmup_worker_still_running", active_requests,
                    )
                # Retrieve a possible late worker exception after a timeout/cancel.
                try:
                    self._worker_task.exception()
                except asyncio.CancelledError:
                    pass
                self._worker_task = None
            if self._status == "completed":
                return self._report(
                    session_id, trigger, "completed", "already_warm", active_requests
                )
            if self._attempted:
                return self._report(
                    session_id,
                    trigger,
                    self._status,
                    "warmup_already_attempted",
                    active_requests,
                )
            if active_requests > 0:
                self._last_trigger_at.pop((session_id, trigger), None)
                return self._report(
                    session_id, trigger, "skipped", "active_user_request", active_requests
                )
            metrics = self.diagnostics.resource_metrics(active_requests)
            available_memory = metrics.get("available_memory_mb")
            if isinstance(available_memory, (int, float)) and available_memory < 32:
                self._last_trigger_at.pop((session_id, trigger), None)
                return self._report(
                    session_id, trigger, "skipped", "low_available_memory", active_requests
                )

            self._attempted = True
            self._status = "running"
            self._trigger = trigger
            self._task = asyncio.create_task(self._run(trigger, session_id, active_requests))
            task = self._task

        self._report(session_id, trigger, "running", "warmup_started", active_requests)
        if wait:
            await task
            return self.snapshot()
        return self.snapshot()

    async def _run(
        self,
        trigger: str,
        session_id: str,
        active_requests: int,
    ) -> None:
        started = time.perf_counter()
        self.diagnostics.record_event({
            "event": "warmup_started",
            "session_id": session_id,
            "trigger": trigger,
            "warmup_status": "running",
            "process_uptime_sec": round(started - self.diagnostics.started_monotonic, 3),
            "prewarm_mode": "lightweight",
            "cpu_experiment_enabled": False,
            "cpu_experiment_requested": _cpu_experiment_requested(),
            "runtime_resources": self.diagnostics.resource_metrics(active_requests),
        })
        worker_task = asyncio.create_task(asyncio.to_thread(self._warmup))
        self._worker_task = worker_task
        try:
            # Shield the thread-backed task: wait_for must not cancel its asyncio
            # wrapper, because the underlying thread cannot be stopped.
            await asyncio.wait_for(
                asyncio.shield(worker_task),
                timeout=_WARMUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._status = "failed"
            self._attempted = False
            self._duration_ms = round((time.perf_counter() - started) * 1000, 3)
            self.diagnostics.record_event({
                "event": "warmup_failed",
                "session_id": session_id,
                "trigger": trigger,
                "warmup_status": "failed",
                "warmup_duration_ms": self._duration_ms,
                "failure_type": "TimeoutError",
                "prewarm_mode": "lightweight",
                "runtime_resources": self.diagnostics.resource_metrics(active_requests),
            })
        except asyncio.CancelledError:
            self._status = "cancelled"
            self._attempted = False
            self._duration_ms = round((time.perf_counter() - started) * 1000, 3)
            self.diagnostics.record_event({
                "event": "warmup_cancelled",
                "session_id": session_id,
                "trigger": trigger,
                "warmup_status": "cancelled",
                "warmup_duration_ms": self._duration_ms,
                "failure_type": "CancelledError",
                "prewarm_mode": "lightweight",
                "runtime_resources": self.diagnostics.resource_metrics(active_requests),
            })
            raise
        except Exception as exc:
            self._status = "failed"
            self._attempted = False
            self._duration_ms = round((time.perf_counter() - started) * 1000, 3)
            self.diagnostics.record_event({
                "event": "warmup_failed",
                "session_id": session_id,
                "trigger": trigger,
                "warmup_status": "failed",
                "warmup_duration_ms": self._duration_ms,
                "failure_type": type(exc).__name__,
                "prewarm_mode": "lightweight",
                "runtime_resources": self.diagnostics.resource_metrics(active_requests),
            })
        else:
            self._worker_task = None
            self._status = "completed"
            self._duration_ms = round((time.perf_counter() - started) * 1000, 3)
            self._completed_at = time.time()
            self._completed_monotonic = time.perf_counter()
            self.diagnostics.record_event({
                "event": "warmup_completed",
                "session_id": session_id,
                "trigger": trigger,
                "warmup_status": "completed",
                "warmup_duration_ms": self._duration_ms,
                "process_uptime_sec": round(time.perf_counter() - self.diagnostics.started_monotonic, 3),
                "prewarm_mode": "lightweight",
                "runtime_resources": self.diagnostics.resource_metrics(active_requests),
            })

    def _report(
        self,
        session_id: str,
        trigger: str,
        status: str,
        reason: str,
        active_requests: int,
    ) -> dict[str, Any]:
        self.diagnostics.record_trigger(
            session_id,
            trigger,
            warmup_status=status,
            reason=reason,
            warmup_duration_ms=self._duration_ms,
            active_requests=active_requests,
        )
        return {**self.snapshot(), "reason": reason}
