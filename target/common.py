"""Shared runtime for the target services: HTTP server, JSON logs, live metrics."""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WINDOW_S = 5.0  # metrics describe the last few seconds, like a rate() over a short window
Handler = Callable[[dict[str, str]], tuple[int, dict]]


def log(service: str, level: str, msg: str) -> None:
    """One JSON object per line on stdout. The supervisor redirects stdout to a file."""
    sys.stdout.write(json.dumps({"ts": time.time(), "service": service, "level": level, "msg": msg}) + "\n")
    sys.stdout.flush()


def rss_mb() -> float:
    """Resident memory of this process, read from the kernel."""
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


class Metrics:
    """Sliding-window request statistics plus process memory and CPU."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reqs: deque[tuple[float, float, bool]] = deque()  # (time, latency ms, ok)
        self._cpu = (time.time(), time.process_time())

    def record(self, latency_ms: float, ok: bool) -> None:
        with self._lock:
            self._reqs.append((time.time(), latency_ms, ok))

    def snapshot(self) -> dict[str, float]:
        now = time.time()
        with self._lock:
            while self._reqs and self._reqs[0][0] < now - WINDOW_S:
                self._reqs.popleft()
            items = list(self._reqs)
        n = len(items)
        lats = sorted(latency for _, latency, _ in items)
        t_prev, cpu_prev = self._cpu
        cpu_now = time.process_time()
        self._cpu = (now, cpu_now)
        return {
            "service_error_rate": 100.0 * sum(1 for *_, ok in items if not ok) / n if n else 0.0,
            "service_latency_p95_ms": lats[int(0.95 * (n - 1))] if n else 0.0,
            "service_request_rate": n / WINDOW_S,
            "process_resident_memory_mb": rss_mb(),
            "process_cpu_percent": 100.0 * (cpu_now - cpu_prev) / max(now - t_prev, 1e-6),
        }

    def prometheus_text(self) -> str:
        """The plain-text exposition format Prometheus scrapes."""
        lines = []
        for name, value in self.snapshot().items():
            lines += [f"# TYPE {name} gauge", f"{name} {value:.3f}"]
        return "\n".join(lines) + "\n"


def parse_prometheus(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition(" ")
            out[name] = float(value)
    return out


def serve(name: str, port: int, routes: dict[str, Handler]) -> None:
    metrics = Metrics()

    class Req(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep connections open between requests
        timeout = 60  # drop idle connections
        disable_nagle_algorithm = True  # headers and body go out as separate small writes
        def log_message(self, *args) -> None:  # silence the default access log
            pass

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/health":
                return self._send(200, b'{"status":"ok"}')
            if url.path == "/metrics":
                return self._send(200, metrics.prometheus_text().encode(), "text/plain")
            if url.path == "/_ctl/log":  # ask this process to emit one line, as an app might
                log(name, query.get("level", "INFO"), query.get("msg", ""))
                return self._send(200, b"{}")
            if url.path == "/_ctl/crash":  # simulate a hard crash (non-zero exit), for deployment tests
                self._send(200, b"{}")
                log(name, "ERROR", "fatal: simulated crash, exiting with code 137")
                os._exit(137)
            fn = routes.get(url.path)
            if fn is None:
                return self._send(404, b'{"error":"not found"}')
            started = time.perf_counter()
            try:
                status, body = fn(query)
            except Exception as exc:  # noqa: BLE001 - a crashing handler is a 500, as in any web server
                log(name, "ERROR", f"{type(exc).__name__}: {exc}")
                status, body = 500, {"error": type(exc).__name__}
            ms = (time.perf_counter() - started) * 1000
            if not url.path.startswith("/_ctl"):  # control calls are not user traffic
                metrics.record(ms, status < 500)
                if random.random() < 0.025:
                    log(name, "INFO", f"GET {url.path} {status} {ms:.0f}ms")
            self._send(status, json.dumps(body).encode())

    class Server(ThreadingHTTPServer):
        request_queue_size = 128  # the default of 5 drops connections under load (1 s TCP retry each)

    server = Server((os.environ.get("BIND_HOST", "127.0.0.1"), port), Req)
    server.daemon_threads = True
    log(name, "INFO", f"{name} {os.environ.get('APP_VERSION', 'v1.0.0')} listening on :{port}")
    server.serve_forever()
