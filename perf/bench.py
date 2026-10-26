"""Load benchmark for the real stack: how much traffic it carries, how fast, and where time goes.

    python -m perf.bench --label baseline
    python -m perf.bench --levels 1,4,16 --seconds 6 --rows 200000 --label try-pooling

For each concurrency level it drives /checkout through the gateway for a few seconds and records
throughput, latency percentiles and errors, plus each service's CPU use and memory. Results are
saved to perf/results/<label>.json. The load generator runs on the same machine as the stack,
so absolute numbers depend on the machine; compare labels run on the SAME machine.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from target.stack import Stack

TICK = os.sysconf("SC_CLK_TCK")
RESULTS = Path(__file__).resolve().parent / "results"


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / TICK  # utime + stime


def pct(sorted_values: list[float], p: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(p / 100 * len(sorted_values)))] if sorted_values else 0.0


def run_level(stack: Stack, concurrency: int, seconds: float) -> dict:
    url = stack.url("api-gateway") + "/checkout"
    lats: list[float] = []
    errors = 0
    lock = threading.Lock()
    stop = time.perf_counter() + seconds

    def worker() -> None:
        nonlocal errors
        mine: list[float] = []
        bad = 0
        while time.perf_counter() < stop:
            t = time.perf_counter()
            try:
                urllib.request.urlopen(url, timeout=10).read()
            except (urllib.error.URLError, OSError):
                bad += 1
            mine.append((time.perf_counter() - t) * 1000)
        with lock:
            lats.extend(mine)
            errors += bad

    cpu0 = {n: cpu_seconds(p.pid) for n, p in stack.procs.items()}
    started = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started
    cpu = {n: round(100 * (cpu_seconds(p.pid) - cpu0[n]) / wall, 1) for n, p in stack.procs.items()}
    inner = {}
    for n in stack.procs:
        snap = stack.scrape(n) or {}
        inner[n] = {"p95_ms": round(snap.get("service_latency_p95_ms", 0), 1),
                    "rss_mb": round(snap.get("process_resident_memory_mb", 0), 1)}
    lats.sort()
    return {
        "concurrency": concurrency, "requests": len(lats), "rps": round(len(lats) / wall, 1),
        "p50_ms": round(statistics.median(lats), 1), "p95_ms": round(pct(lats, 95), 1),
        "p99_ms": round(pct(lats, 99), 1), "max_ms": round(lats[-1], 1),
        "error_pct": round(100 * errors / max(1, len(lats)), 2),
        "cpu_pct": cpu, "services": inner,
    }


def run_bench(levels: list[int], seconds: float, rows: int, warmup: float = 2.0) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "w"
        work.mkdir()
        stack = Stack(work, db_rows=rows)
        stack.start(db_path=Path(tmp) / f"shop-{rows}.sqlite")
        try:
            run_level(stack, 2, warmup)  # warm caches and connections, result discarded
            return [run_level(stack, c, seconds) for c in levels]
        finally:
            stack.stop()


def table(results: list[dict]) -> str:
    head = f"{'conc':>4} {'req/s':>7} {'p50':>7} {'p95':>7} {'p99':>7} {'err%':>5}  cpu% (gw / orders / inv / db)"
    rows = [head]
    for r in results:
        c = r["cpu_pct"]
        rows.append(f"{r['concurrency']:>4} {r['rps']:>7} {r['p50_ms']:>7} {r['p95_ms']:>7} {r['p99_ms']:>7} "
                    f"{r['error_pct']:>5}  {c['api-gateway']} / {c['orders-service']} / "
                    f"{c['inventory-service']} / {c['orders-db']}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--levels", default="1,2,4,8,16,32")
    p.add_argument("--seconds", type=float, default=6)
    p.add_argument("--rows", type=int, default=200_000)
    p.add_argument("--label", default="run")
    args = p.parse_args(argv)
    levels = [int(x) for x in args.levels.split(",")]
    results = run_bench(levels, args.seconds, args.rows)
    print(table(results))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.label}.json"
    out.write_text(json.dumps({"label": args.label, "cpus": os.cpu_count(), "rows": args.rows,
                               "seconds": args.seconds, "results": results}, indent=1) + "\n")
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
