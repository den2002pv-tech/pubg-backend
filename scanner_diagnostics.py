from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
from contextlib import contextmanager
from pathlib import Path
import re
import resource
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


LOGGER = logging.getLogger(__name__)
_FILE_NAME_RE = re.compile(
    r"scanner-events-\d{8}-\d+-[a-f0-9]{8}-\d{4}\.jsonl\Z"
)
_SENSITIVE_KEYS = {
    "authorization", "body", "cookie", "databaseurl", "file", "filename",
    "headers", "password", "secret", "telegramid", "token", "userid",
    "username", "user", "image", "imagedata",
}


def _positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            name = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if name in _SENSITIVE_KEYS:
                continue
            safe[str(key)[:80]] = _safe_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:200]]
    return None


class ResourceSampler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._previous_cpu: float | None = None
        self._previous_time: float | None = None

    def snapshot(
        self,
        directory: Path,
        active_requests: int,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.perf_counter()
        cpu_total: float | None = None
        if source is not None:
            user_cpu = source.get("user_cpu_seconds")
            system_cpu = source.get("system_cpu_seconds")
            if isinstance(user_cpu, (int, float)) and isinstance(system_cpu, (int, float)):
                cpu_total = float(user_cpu + system_cpu)
        else:
            try:
                usage = resource.getrusage(resource.RUSAGE_SELF)
                cpu_total = float(usage.ru_utime + usage.ru_stime)
            except (OSError, AttributeError, ValueError):
                pass

        with self._lock:
            cpu_percent = None
            cpu_interval = None
            if (
                cpu_total is not None
                and self._previous_cpu is not None
                and self._previous_time is not None
                and now > self._previous_time
            ):
                cpu_interval = now - self._previous_time
                cpu_percent = round(
                    max(0.0, cpu_total - self._previous_cpu)
                    / cpu_interval * 100,
                    2,
                )
            self._previous_cpu = cpu_total
            self._previous_time = now

        rss_bytes = source.get("rss_bytes") if source is not None else None
        vms_bytes = source.get("vms_bytes") if source is not None else None
        if source is None:
            try:
                for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
                    if line.startswith("VmRSS:"):
                        rss_bytes = int(line.split()[1]) * 1024
                    elif line.startswith("VmSize:"):
                        vms_bytes = int(line.split()[1]) * 1024
            except (OSError, ValueError, IndexError):
                pass

        memory_limit = (
            source.get("memory_limit_bytes")
            if source is not None else _read_cgroup_value(
                ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
            )
        )
        memory_current = (
            source.get("container_memory_bytes")
            if source is not None else _read_cgroup_value(
                ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes")
            )
        )
        available_memory = (
            max(0, memory_limit - memory_current) / (1024 * 1024)
            if memory_limit is not None and memory_current is not None
            else None
        )
        disk_free = disk_used = None
        try:
            disk = shutil.disk_usage(directory if directory.exists() else directory.parent)
            disk_free = round(disk.free / (1024 * 1024), 2)
            disk_used = round(disk.used / (1024 * 1024), 2)
        except OSError:
            pass

        return {
            "cpu_percent": cpu_percent,
            "cpu_percent_interval_sec": round(cpu_interval, 3) if cpu_interval else None,
            "memory_rss_mb": round(rss_bytes / (1024 * 1024), 2) if rss_bytes is not None else None,
            "memory_vms_mb": round(vms_bytes / (1024 * 1024), 2) if vms_bytes is not None else None,
            "active_requests": active_requests,
            "active_scan_requests": active_requests,
            "available_memory_mb": round(available_memory, 2) if available_memory is not None else None,
            "disk_free_mb": disk_free,
            "disk_used_mb": disk_used,
            "cpu_limit": (
                source.get("cpu_limit") if source is not None else _read_cpu_limit()
            ),
        }


def _read_cgroup_value(paths: tuple[str, ...]) -> int | None:
    for path in paths:
        try:
            raw = Path(path).read_text(encoding="ascii").strip()
            if raw != "max":
                value = int(raw)
                if 0 < value < 2**60:
                    return value
        except (OSError, ValueError):
            continue
    return None


def _read_cpu_limit() -> float | None:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
        if quota != "max" and int(quota) > 0 and int(period) > 0:
            return round(int(quota) / int(period), 3)
    except (OSError, ValueError):
        pass
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text(encoding="ascii"))
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text(encoding="ascii"))
        if quota > 0 and period > 0:
            return round(quota / period, 3)
    except (OSError, ValueError):
        pass
    return None


class ScannerDiagnosticStore:
    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        enabled: bool | None = None,
        max_total_bytes: int | None = None,
        max_file_bytes: int | None = None,
        retention_days: int | None = None,
        max_report_bytes: int | None = None,
        resource_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        root = Path(__file__).resolve().parent
        self.directory = Path(
            directory
            or os.getenv("SCANNER_DIAGNOSTICS_DIR", str(root / "scanner_diagnostics"))
        ).expanduser().resolve()
        self.enabled = (
            _env_enabled("SCANNER_DIAGNOSTICS_ENABLED", True)
            if enabled is None else enabled
        )
        self.storage_enabled = _env_enabled(
            "SCANNER_DIAGNOSTICS_LOCAL_STORAGE_ENABLED", True
        )
        self.max_total_bytes = (
            max_total_bytes if max_total_bytes is not None
            else _positive_int_env("SCANNER_DIAGNOSTICS_MAX_TOTAL_BYTES", 100 * 1024 * 1024)
        )
        self.max_file_bytes = (
            max_file_bytes if max_file_bytes is not None
            else _positive_int_env("SCANNER_DIAGNOSTICS_MAX_FILE_BYTES", 5 * 1024 * 1024)
        )
        self.retention_days = (
            retention_days if retention_days is not None
            else _positive_int_env("SCANNER_DIAGNOSTICS_RETENTION_DAYS", 7)
        )
        self.max_report_bytes = (
            max_report_bytes if max_report_bytes is not None
            else _positive_int_env("SCANNER_DIAGNOSTICS_MAX_REPORT_BYTES", 10 * 1024 * 1024)
        )
        if min(
            self.max_total_bytes,
            self.max_file_bytes,
            self.retention_days,
            self.max_report_bytes,
        ) < 1:
            raise ValueError("Scanner diagnostics limits must be positive")
        if self.max_total_bytes < self.max_file_bytes:
            raise ValueError("SCANNER_DIAGNOSTICS_MAX_TOTAL_BYTES must be at least the file limit")
        if self.max_report_bytes < 1:
            raise ValueError("SCANNER_DIAGNOSTICS_MAX_REPORT_BYTES must be positive")

        self.run_id = uuid.uuid4().hex
        self.started_at = datetime.now(timezone.utc)
        self.started_monotonic = time.perf_counter()
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._online_users: dict[str, float] = {}
        self._user_hash_key = os.urandom(32)
        self._process_scan_count = 0
        self._resources = ResourceSampler()
        self._resource_provider = resource_provider

    def _runtime_resources(self, active_requests: int) -> dict[str, Any]:
        try:
            source = self._resource_provider() if self._resource_provider is not None else None
            return self._resources.snapshot(self.directory, active_requests, source)
        except Exception as exc:
            LOGGER.warning("Scanner resource metrics unavailable (%s)", type(exc).__name__)
            return {
                "cpu_percent": None,
                "cpu_percent_interval_sec": None,
                "memory_rss_mb": None,
                "memory_vms_mb": None,
                "active_requests": active_requests,
                "active_scan_requests": active_requests,
                "available_memory_mb": None,
                "disk_free_mb": None,
                "disk_used_mb": None,
                "cpu_limit": None,
            }

    def _active(self) -> bool:
        return self.enabled and self.storage_enabled

    def _diagnostic_paths(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(
            (path for path in self.directory.iterdir() if _FILE_NAME_RE.fullmatch(path.name) and path.is_file()),
            key=lambda path: (path.stat().st_mtime, path.name),
        )

    def _prune(self, active_name: str | None = None) -> None:
        paths = self._diagnostic_paths()
        now = time.time()
        cutoff = now - self.retention_days * 86400
        for path in paths:
            try:
                stat = path.stat()
                if path.name != active_name and (
                    stat.st_mtime < cutoff or stat.st_size > self.max_file_bytes
                ):
                    path.unlink()
            except OSError as exc:
                LOGGER.warning("Scanner diagnostics retention cleanup failed (%s)", type(exc).__name__)

        paths = self._diagnostic_paths()
        total = sum(path.stat().st_size for path in paths)
        for path in paths:
            if total <= self.max_total_bytes:
                break
            try:
                stat = path.stat()
                if path.name == active_name:
                    continue
                path.unlink()
                total -= stat.st_size
            except OSError as exc:
                LOGGER.warning("Scanner diagnostics size cleanup failed (%s)", type(exc).__name__)

    @contextmanager
    def _filesystem_lock(self):
        descriptor = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self.directory / ".scanner-diagnostics.lock",
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if descriptor is not None:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(descriptor)

    def _next_path(self, append_size: int) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = f"scanner-events-{today}-{os.getpid()}-{self.run_id[:8]}-"
        candidates = sorted(
            path for path in self.directory.glob(f"{prefix}*.jsonl")
            if _FILE_NAME_RE.fullmatch(path.name)
        )
        if candidates:
            latest = candidates[-1]
            if latest.stat().st_size + append_size <= self.max_file_bytes:
                return latest
            sequence = int(latest.stem.rsplit("-", 1)[1]) + 1
        else:
            sequence = 1
        return self.directory / f"{prefix}{sequence:04d}.jsonl"

    def record_event(self, event: dict[str, Any]) -> bool:
        if not self._active():
            return False
        payload = {
            **event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "worker_pid": os.getpid(),
            "run_id": self.run_id,
        }
        safe_payload = _safe_value(payload)
        try:
            line = (json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Scanner diagnostics serialization failed (%s)", type(exc).__name__)
            return False
        if len(line) > self.max_file_bytes:
            LOGGER.warning("Scanner diagnostics event exceeded configured file size limit")
            return False

        try:
            with self._lock:
                with self._filesystem_lock():
                    self._prune()
                    path = self._next_path(len(line))
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        offset = 0
                        while offset < len(line):
                            offset += os.write(descriptor, line[offset:])
                    finally:
                        os.close(descriptor)
                    self._prune(active_name=path.name)
            return True
        except OSError as exc:
            LOGGER.warning("Scanner diagnostics write failed (%s)", type(exc).__name__)
            return False

    def note_user_online(self, user_id: int) -> bool:
        now = time.monotonic()
        identity = hmac.new(self._user_hash_key, str(user_id).encode("ascii"), hashlib.sha256).hexdigest()
        with self._lock:
            previous = self._online_users.get(identity)
            self._online_users[identity] = now
            for key, seen_at in tuple(self._online_users.items()):
                if now - seen_at > 90:
                    self._online_users.pop(key, None)
            if len(self._online_users) > 10_000:
                oldest = sorted(self._online_users.items(), key=lambda item: item[1])
                for key, _ in oldest[: len(oldest) - 8_000]:
                    self._online_users.pop(key, None)
            is_transition = previous is None or now - previous > 90
        return is_transition

    def record_trigger(
        self,
        session_id: str,
        trigger: str,
        *,
        warmup_status: str | None = None,
        reason: str | None = None,
        warmup_duration_ms: float | None = None,
        active_requests: int = 0,
    ) -> None:
        if trigger not in {
            "application_start", "user_online", "scanner_open", "image_selected",
            "before_scan",
        }:
            return
        now = time.monotonic()
        with self._lock:
            state = self._session_state(session_id, now)
            state["triggers"][trigger] = now
            state["classified_triggers"].discard(trigger)
            state["updated_at"] = now
        self.record_event({
            "event": "prewarm_trigger",
            "session_id": session_id,
            "trigger": trigger,
            "warmup_status": warmup_status or "not_started",
            "warmup_duration_ms": warmup_duration_ms,
            "process_uptime_sec": round(now - self.started_monotonic, 3),
            "prewarm_mode": "lightweight",
            "reason": reason,
            "runtime_resources": self._runtime_resources(active_requests),
        })

    def record_scan(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            state = self._session_state(session_id, now)
            self._process_scan_count += 1
            state["scan_count"] += 1
            elapsed = now - state["last_scan_at"] if state["last_scan_at"] is not None else None
            classifications: list[str] = []
            if self._process_scan_count == 1:
                classifications.append("first_after_process_start")
            for trigger in ("user_online", "scanner_open", "image_selected"):
                if trigger in state["triggers"] and trigger not in state["classified_triggers"]:
                    classifications.append(f"first_after_{trigger}")
                    state["classified_triggers"].add(trigger)
            state["last_scan_at"] = now
            state["updated_at"] = now
            sequence = state["scan_count"]

        interval_bucket = None
        if elapsed is not None:
            nearest = min((5, 10, 30, 60), key=lambda target: abs(target - elapsed))
            if abs(nearest - elapsed) <= max(2.0, nearest * 0.2):
                interval_bucket = nearest
        return {
            **event,
            "event": event.get("event", "scan_completed"),
            "session_id": session_id,
            "scan_number_for_session": sequence,
            "first_scan_classifications": classifications,
            "scan_interval_seconds": round(elapsed, 3) if elapsed is not None else None,
            "scan_interval_target_seconds": interval_bucket,
        }

    def _session_state(self, session_id: str, now: float) -> dict[str, Any]:
        state = self._sessions.get(session_id)
        if state is None:
            state = {
                "scan_count": 0,
                "last_scan_at": None,
                "triggers": {},
                "classified_triggers": set(),
                "updated_at": now,
            }
            self._sessions[session_id] = state
        if len(self._sessions) > 10_000:
            expired = sorted(
                self._sessions.items(), key=lambda item: item[1]["updated_at"]
            )[: len(self._sessions) - 8_000]
            for key, _ in expired:
                self._sessions.pop(key, None)
        return state

    def resource_metrics(self, active_requests: int) -> dict[str, Any]:
        return self._runtime_resources(active_requests)

    def list_files(self) -> list[dict[str, Any]]:
        if not self._active():
            return []
        try:
            with self._lock, self._filesystem_lock():
                self._prune()
                files = self._diagnostic_paths()
                result = []
                for path in files:
                    records = self._read_file(path)
                    last = records[-1] if records else {}
                    result.append({
                        "name": path.name,
                        "type": "jsonl",
                        "created_at": (
                            records[0].get("timestamp")
                            if records else datetime.fromtimestamp(
                                path.stat().st_mtime, timezone.utc
                            ).isoformat()
                        ),
                        "modified_at": datetime.fromtimestamp(
                            path.stat().st_mtime, timezone.utc
                        ).isoformat(),
                        "size_bytes": path.stat().st_size,
                        "record_count": len(records),
                        "run_id": last.get("run_id"),
                        "worker_pid": last.get("worker_pid"),
                        "session_id": last.get("session_id"),
                        "download_available": True,
                    })
                return result
        except OSError as exc:
            LOGGER.warning("Scanner diagnostics listing failed (%s)", type(exc).__name__)
            return []

    def safe_file(self, name: str) -> Path | None:
        if not _FILE_NAME_RE.fullmatch(name):
            return None
        try:
            root = self.directory.resolve(strict=True)
            candidate = (root / name).resolve(strict=True)
            if candidate.parent != root or not candidate.is_file():
                return None
            if candidate.stat().st_size > self.max_file_bytes:
                return None
            return candidate
        except OSError:
            return None

    def read_records(self) -> list[dict[str, Any]]:
        if not self._active():
            return []
        records: list[dict[str, Any]] = []
        total_bytes = 0
        try:
            with self._lock, self._filesystem_lock():
                for path in reversed(self._diagnostic_paths()):
                    size = path.stat().st_size
                    if total_bytes + size > self.max_report_bytes:
                        break
                    records.extend(self._read_file(path))
                    total_bytes += size
        except OSError as exc:
            LOGGER.warning("Scanner diagnostics read failed (%s)", type(exc).__name__)
        return records

    @staticmethod
    def _read_file(path: Path) -> list[dict[str, Any]]:
        records = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError as exc:
            LOGGER.warning("Scanner diagnostics file read failed (%s)", type(exc).__name__)
        return records

    def summary(self) -> dict[str, Any]:
        records = self.read_records()
        scans = [row for row in records if row.get("event") in {"scan_completed", "scan_failed"}]
        completed = [row for row in scans if row.get("http_status", 500) < 400]
        first = [
            row for row in completed
            if row.get("first_scan_classifications")
        ]
        subsequent = [
            row for row in completed
            if not row.get("first_scan_classifications")
        ]

        def aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
            values = sorted(
                float(row[key]) for row in rows
                if isinstance(row.get(key), (int, float)) and math.isfinite(float(row[key]))
            )
            if not values:
                return {"count": 0, "avg_ms": None, "median_ms": None, "p90_ms": None, "p95_ms": None, "max_ms": None}

            def percentile(percent: int) -> float:
                return round(values[max(0, math.ceil(percent / 100 * len(values)) - 1)], 3)

            middle = len(values) // 2
            median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
            return {
                "count": len(values),
                "avg_ms": round(sum(values) / len(values), 3),
                "median_ms": round(median, 3),
                "p90_ms": percentile(90),
                "p95_ms": percentile(95),
                "max_ms": round(values[-1], 3),
            }

        durations = {
            "first_scans": aggregate(first, "total_server_ms"),
            "subsequent_scans": aggregate(subsequent, "total_server_ms"),
        }
        trigger_results = {
            trigger: aggregate(
                [
                    row for row in completed
                    if f"first_after_{trigger}" in row.get("first_scan_classifications", [])
                ],
                "total_server_ms",
            )
            for trigger in ("user_online", "scanner_open", "image_selected")
        }
        interval_results = {
            f"{seconds}s": aggregate(
                [
                    row for row in subsequent
                    if row.get("scan_interval_target_seconds") == seconds
                ],
                "total_server_ms",
            )
            for seconds in (5, 10, 30, 60)
        }
        stage_names = (
            "request_body_read_ms", "queue_wait_ms", "upload_file_read_ms",
            "image_decode_ms", "upload_rgb_convert_ms", "image_preprocess_ms",
            "detection_ms", "counter_ms", "raw_hash_ms", "clean_hash_ms",
            "hashing_total_ms", "catalog_load_ms", "matching_ms",
            "matching_evaluation_ms", "quality_check_ms", "response_build_ms",
            "server_total_ms", "total_server_ms",
        )
        client_results = [
            row for row in records if row.get("event") == "scan_client_result"
        ]
        client_timing_names = (
            "client_form_data_ms",
            "client_wake_ms",
            "client_upload_ms",
            "client_response_wait_ms",
            "client_response_parse_ms",
            "client_request_ms",
            "client_result_render_ms",
            "client_scan_to_result_ms",
        )

        return {
            "scan_count": len(scans),
            "successful_scans": len(completed),
            "client_result_count": len(client_results),
            "client_result_timings": {
                name: aggregate(client_results, name)
                for name in client_timing_names
            },
            "error_count": len(scans) - len(completed),
            "first_and_subsequent": durations,
            "first_scan_by_trigger": trigger_results,
            "subsequent_by_interval": interval_results,
            "stage_durations": {
                stage: aggregate(scans, stage) for stage in stage_names
            },
            "process_start_count": sum(row.get("event") == "worker_start" for row in records),
            "warmup_duration": aggregate(
                [row for row in records if row.get("event") == "warmup_completed"],
                "warmup_duration_ms",
            ),
            "triggers": {
                trigger: sum(
                    (
                        row.get("event") == "prewarm_trigger"
                        and row.get("trigger") == trigger
                    )
                    or (
                        trigger == "before_scan"
                        and row.get("event") in {"scan_completed", "scan_failed"}
                        and row.get("prewarm_check_trigger") == trigger
                    )
                    for row in records
                )
                for trigger in ("application_start", "user_online", "scanner_open", "image_selected", "before_scan")
            },
            "warmup_vs_no_warmup": {
                "completed": aggregate(
                    [row for row in completed if row.get("warmup_status_at_scan") == "completed"],
                    "total_server_ms",
                ),
                "not_completed": aggregate(
                    [row for row in completed if row.get("warmup_status_at_scan") != "completed"],
                    "total_server_ms",
                ),
            },
            "cpu_experiment": {
                "enabled": False,
                "implemented": False,
                "reason": "Enzonic resource policy is not verifiable from repository configuration",
            },
            "retention_days": self.retention_days,
            "max_total_bytes": self.max_total_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_report_bytes": self.max_report_bytes,
            "storage_directory": str(self.directory),
            "storage_enabled": self._active(),
        }


def _env_enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
