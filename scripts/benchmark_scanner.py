"""Offline scan-handler baseline; no application startup, network, or database writes.

Run from the checkout with the project's Python environment. Each invocation is
one fresh process; repeat cold invocations to measure first-request variability.
Outputs include AI provenance. Timers nest: never sum all profile fields.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib.metadata import version
from io import BytesIO
import json
import math
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def summarize(values):
    ordered = sorted(values)
    if not ordered:
        return {}
    def percentile(q):
        pos = (len(ordered) - 1) * q
        lo = math.floor(pos)
        return ordered[lo] + (ordered[math.ceil(pos)] - ordered[lo]) * (pos - lo)
    return {"n": len(values), "median": percentile(.5), "p90": percentile(.9),
            "p95": percentile(.95), "max": ordered[-1]}


def digest(data):
    return hashlib.sha256(data).hexdigest()


async def benchmark(args):
    # Fixed offline configuration, local to this benchmark process only.
    os.environ["SCANNER_PREWARM_ENABLED"] = "false"
    os.environ["SCAN_RATE_LIMIT"] = str(1_000_000)
    os.environ.pop("SENTRY_DSN", None)
    started = time.perf_counter()
    import main
    import cv2
    from starlette.datastructures import UploadFile
    from starlette.requests import Request
    import_ms = (time.perf_counter() - started) * 1000
    manifest_path = ROOT / "tests/fixtures/screenshots/cases.json"
    cases = json.loads(manifest_path.read_text())["cases"]
    if not cases:
        raise ValueError("No screenshot fixtures; cannot produce a baseline")
    inputs = [(case, (ROOT / case["image_path"]).read_bytes()) for case in cases]
    signatures = {}
    samples = []
    batch_metrics = {}

    async def scan_one(case, data, phase):
        request = Request({"type": "http", "method": "POST", "path": "/scan",
                           "headers": [], "client": ("offline-benchmark", 0)})
        upload = UploadFile(BytesIO(data), size=len(data), filename="fixture.png")
        start = time.perf_counter()
        try:
            result = await main.scan(request, upload, init_data="")
        finally:
            await upload.close()
        serialization_start = time.perf_counter()
        encoded = json.dumps(result, ensure_ascii=False).encode()
        serialization_ms = (time.perf_counter() - serialization_start) * 1000
        elapsed_ms = (time.perf_counter() - start) * 1000
        decisions = [{k: card.get(k) for k in ("id", "quantity", "duplicates", "row", "col", "distance")}
                     for card in result["cards"]]
        signature = digest(json.dumps(decisions, sort_keys=True).encode())
        previous = signatures.setdefault(case["id"], signature)
        owned = [c for c in case["cards"] if c.get("state", "owned") == "owned"]
        samples.append({"case": case["id"], "phase": phase,
                        "handler_and_json_ms": elapsed_ms, "json_ms": serialization_ms,
                        "response_bytes": len(encoded), "input_bytes": len(data),
                        "cards": result["count"], "expected_owned_cards": len(owned),
                        "repeat_decisions_equal": previous == signature,
                        "decisions_sha256": signature, "decisions": decisions,
                        "profile": result["profile"]})

    async def batch(items, phase, clients):
        # Bound outstanding requests independently of the server semaphore.
        gate = asyncio.Semaphore(clients)
        async def limited(item):
            async with gate:
                await scan_one(*item, phase)
        lags = []
        async def monitor_loop():
            while True:
                tick = time.perf_counter()
                await asyncio.sleep(.01)
                lags.append(max(0, (time.perf_counter() - tick) * 1000 - 10))
        monitor = asyncio.create_task(monitor_loop())
        try:
            await asyncio.gather(*(limited(item) for item in items))
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        batch_metrics[phase] = {
            "loop_lag_ms": summarize(lags),
            "process_rss_after_mib": main.SCANNER_DIAGNOSTICS.resource_metrics(0).get("memory_rss_mb"),
            "process_peak_rss_so_far_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        }

    await scan_one(*inputs[0], "first_request")
    await batch(inputs, "warmup_corpus", 1)
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    await batch(inputs * args.repeats, "warm", 1)
    warm_cpu_ms = (time.process_time() - cpu_start) * 1000
    warm_wall_ms = (time.perf_counter() - wall_start) * 1000
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    await batch(inputs * args.repeats, "concurrent", args.clients)
    concurrent_cpu_ms = (time.process_time() - cpu_start) * 1000
    concurrent_wall_ms = (time.perf_counter() - wall_start) * 1000
    summaries = {}
    for phase in ("first_request", "warmup_corpus", "warm", "concurrent"):
        rows = [s for s in samples if s["phase"] == phase]
        metrics = {"handler_and_json_ms": summarize([r["handler_and_json_ms"] for r in rows]),
                   "json_ms": summarize([r["json_ms"] for r in rows])}
        for key in sorted({k for row in rows for k in row["profile"] if k.endswith("_ms")}):
            metrics[key] = summarize([r["profile"][key] for r in rows
                                      if isinstance(r["profile"].get(key), (float, int))])
        summaries[phase] = metrics
    files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    fingerprints = {p: digest((ROOT / p).read_bytes()) for p in files if (ROOT / p).is_file() and "__pycache__" not in Path(p).parts and not p.endswith(".pyc")}
    fingerprints["scripts/benchmark_scanner.py"] = digest(Path(__file__).read_bytes())
    return {"_provenance": "Generated by CodeRabbit; offline benchmark measurements",
            "scope": "Direct anonymous scan handler + Python JSON; excludes HTTP/multipart, startup lifespan, PostgreSQL, network and browser. Prewarm disabled. Not end-to-end latency.",
            "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "versions": {p: version(p) for p in ("Pillow", "numpy", "scipy", "ImageHash", "opencv-python-headless", "fastapi")},
            "python": platform.python_version(), "platform": platform.platform(),
            "cpu_count": os.cpu_count(), "cpu_info": Path("/proc/cpuinfo").read_text().split("\n\n")[0],
            "memory_info": Path("/proc/meminfo").read_text().splitlines()[:3],
            "cgroup_cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text() if Path("/sys/fs/cgroup/cpu.max").exists() else None,
            "cgroup_memory_max": Path("/sys/fs/cgroup/memory.max").read_text() if Path("/sys/fs/cgroup/memory.max").exists() else None,
            "import_ms": import_ms, "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "warm_batch_cpu_ms": warm_cpu_ms, "concurrent_batch_cpu_ms": concurrent_cpu_ms,
            "warm_batch_wall_ms": warm_wall_ms, "concurrent_batch_wall_ms": concurrent_wall_ms,
            "catalog_size": len(main.load_db()), "cv2_threads": cv2.getNumThreads(),
            "scan_concurrency": main.SCAN_CONCURRENCY, "clients": args.clients, "repeats": args.repeats,
            "fingerprints": fingerprints, "batch_metrics": batch_metrics, "summaries": summaries, "samples": samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--clients", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1 or args.clients < 1:
        parser.error("repeats and clients must be positive")
    os.chdir(ROOT)
    report = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("import_ms", "peak_process_rss_mib", "catalog_size")}))


if __name__ == "__main__":
    main()
