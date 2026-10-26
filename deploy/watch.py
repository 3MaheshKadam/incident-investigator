"""Watch a running deployment with the same detector used everywhere else.

    python -m deploy.watch                  # print incidents until Ctrl-C
    python -m deploy.watch --seconds 120

It scrapes each service's /metrics, builds the same metric series as the live episodes, and runs
`monitor.detect` on them. The detector learns normal from the first samples, so start it while
the system is healthy and carrying traffic (`docker compose --profile load up -d`).
"""

from __future__ import annotations

import argparse
import threading
import time
import urllib.request

from deploy.docker_stack import PUBLISHED
from monitor.detect import Incident, detect_incidents
from shop.system import METRICS
from target.common import parse_prometheus
from target.episode import LIVE_CONFIG, SAMPLE_S


class Watcher:
    def __init__(self, ports: dict[str, int] | None = None, interval: float = SAMPLE_S):
        self.ports = ports or dict(PUBLISHED)
        self.interval = interval
        self.metrics: dict[str, dict[str, list[float]]] = {n: {m: [] for m in METRICS} for n in self.ports}
        self._last_mem = {n: 0.0 for n in self.ports}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def samples(self) -> int:
        return len(next(iter(self.metrics.values()))["error_rate"])

    def poll(self) -> None:
        for name, port in self.ports.items():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=0.4) as r:
                    snap = parse_prometheus(r.read().decode())
                self._last_mem[name] = snap["process_resident_memory_mb"]
                values = (snap["service_error_rate"], snap["service_latency_p95_ms"],
                          snap["process_resident_memory_mb"], snap["process_cpu_percent"],
                          snap["service_request_rate"])
            except OSError:  # not answering: what an "up == 0" alert would see
                values = (100.0, 0.0, self._last_mem[name], 0.0, 0.0)
            for metric, v in zip(METRICS, values, strict=True):
                self.metrics[name][metric].append(round(v, 2))

    def incidents(self) -> list[Incident]:
        return detect_incidents(self.metrics, [], [], LIVE_CONFIG)

    def start(self) -> None:
        def loop() -> None:
            nxt = time.time()
            while not self._stop.is_set():
                self.poll()
                nxt += self.interval
                time.sleep(max(0.0, nxt - time.time()))
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--seconds", type=float, default=0, help="stop after this long (default: until Ctrl-C)")
    args = p.parse_args(argv)
    w = Watcher()
    w.start()
    seen: set[str] = set()
    started = time.time()
    print(f"watching {', '.join(w.ports)} (learning normal for the first {LIVE_CONFIG.train} samples)")
    try:
        while not args.seconds or time.time() - started < args.seconds:
            time.sleep(1)
            for inc in w.incidents():
                if inc.id not in seen:
                    seen.add(inc.id)
                    print(f"ALERT {inc.alert}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        w.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
