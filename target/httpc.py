"""A tiny HTTP client that keeps connections open between calls.

`urllib` opens a new TCP connection for every request. Between services that call each other
many times a second that costs CPU and sockets. This client reuses connections per host and
raises the same exceptions `urllib` would (URLError for refused, HTTPError for 4xx/5xx,
TimeoutError), so callers do not change how they handle failures.
"""

from __future__ import annotations

import http.client
import json
import queue
import threading
import urllib.error
from urllib.parse import urlsplit

MAX_IDLE_PER_HOST = 32
_pools: dict[tuple[str, int], queue.LifoQueue[http.client.HTTPConnection]] = {}
_lock = threading.Lock()


def _pool(host: str, port: int) -> queue.LifoQueue[http.client.HTTPConnection]:
    with _lock:
        return _pools.setdefault((host, port), queue.LifoQueue(MAX_IDLE_PER_HOST))


def _request(conn: http.client.HTTPConnection, path: str, timeout: float) -> tuple[int, str, object, bytes]:
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    conn.timeout = timeout
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    return resp.status, resp.reason, resp.headers, body


def get_json(url: str, timeout: float) -> tuple[int, dict]:
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 80
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    pool = _pool(host, port)
    reused = True
    try:
        conn = pool.get_nowait()
    except queue.Empty:
        conn, reused = http.client.HTTPConnection(host, port, timeout=timeout), False
    while True:
        try:
            status, reason, headers, body = _request(conn, path, timeout)
            break
        except (ConnectionError, http.client.HTTPException, OSError) as exc:
            conn.close()
            if isinstance(exc, TimeoutError):
                raise
            if reused:  # an idle connection the server closed: try once on a fresh one
                conn, reused = http.client.HTTPConnection(host, port, timeout=timeout), False
                continue
            if isinstance(exc, ConnectionRefusedError):
                raise urllib.error.URLError(exc) from None
            raise
    if status >= 400:
        if not _release(pool, conn, headers):
            conn.close()
        raise urllib.error.HTTPError(url, status, reason, headers, None)  # type: ignore[arg-type]
    if not _release(pool, conn, headers):
        conn.close()
    return status, json.loads(body or b"{}")


def _release(pool: queue.LifoQueue[http.client.HTTPConnection], conn: http.client.HTTPConnection,
             headers: object) -> bool:
    if str(getattr(headers, "get", lambda *_: "")("Connection", "")).lower() == "close":
        return False
    try:
        pool.put_nowait(conn)
    except queue.Full:
        return False
    return True
