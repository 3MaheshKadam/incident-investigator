"""The four target services. Run one with:  python -m target.services <name>

    api-gateway  ->  orders-service  ->  orders-db (SQLite)
                 ->  inventory-service

Each gets its settings from environment variables, set by the supervisor (stack.py).
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

from target.common import Handler, log, serve
from target.db import INDEX, QUERY
from target.httpc import get_json

SLOW_QUERY_MS = 60.0


# ----- orders-db: a small SQL service over a real SQLite file ----------------------------------
def orders_db(port: int) -> dict[str, Handler]:
    path = os.environ["DB_PATH"]
    # Reuse connections across requests, but keep only a few: every open SQLite connection holds
    # a page cache, so an unbounded pool grows with the busiest moment the service has ever seen.
    pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(int(os.environ.get("DB_POOL_SIZE", "4")))

    def query(q: dict[str, str]) -> tuple[int, dict]:
        try:
            con = pool.get_nowait()
        except queue.Empty:
            con = sqlite3.connect(path, check_same_thread=False)
        try:
            started = time.perf_counter()
            rows = con.execute(QUERY, (q.get("email", ""),)).fetchall()
            ms = (time.perf_counter() - started) * 1000
            if ms > SLOW_QUERY_MS:
                plan = con.execute("EXPLAIN QUERY PLAN " + QUERY, (q.get("email", ""),)).fetchall()
                log("orders-db", "WARN", f"slow query ({ms:.0f}ms): {QUERY}; plan: {plan[0][3]}")
        except BaseException:
            con.close()  # never return a connection in an unknown state
            raise
        try:
            pool.put_nowait(con)
        except queue.Full:
            con.close()
        return 200, {"rows": len(rows)}

    def drop_index(_: dict[str, str]) -> tuple[int, dict]:
        con = sqlite3.connect(path)
        con.execute(f"DROP INDEX IF EXISTS {INDEX}")
        con.commit()
        con.close()
        return 200, {"dropped": INDEX}

    return {"/query": query, "/_ctl/drop_index": drop_index}


# ----- orders-service: business logic; the place a bad deploy or a flag shows up ---------------
def orders_service(port: int) -> dict[str, Handler]:
    db = os.environ["ORDERS_DB_URL"]
    flags_path = os.environ["FLAGS_PATH"]
    buggy = os.environ.get("BUG") == "1"
    counter = itertools.count(1)

    def flag_on(name: str) -> bool:
        try:
            with open(flags_path) as f:
                return bool(json.load(f).get(name))
        except (OSError, ValueError):
            return False

    def order(q: dict[str, str]) -> tuple[int, dict]:
        n = next(counter)
        if flag_on("fast_checkout_path") and n % 6 == 0:
            time.sleep(0.4)
            raise TimeoutError("timeout in experimental code path (call to model-v2 after 2000ms)")
        rows = 0
        for i in range(3):
            _, body = get_json(f"{db}/query?email=user{(n * 7 + i * 13) % 50000}@example.com", 5.0)
            rows += body["rows"]
        if buggy and n % 5 == 0:
            # The bug shipped in this release: the currency lookup returns nothing for some orders.
            currency = None
            return 200, {"total": rows, "currency": currency.upper()}  # AttributeError
        return 200, {"orders": rows}

    return {"/order": order}


# ----- inventory-service: simple, and the place a memory leak is injected ----------------------
def inventory_service(port: int) -> dict[str, Handler]:
    hoard: list[bytearray] = []

    def stock(_: dict[str, str]) -> tuple[int, dict]:
        return 200, {"in_stock": True}

    def leak(q: dict[str, str]) -> tuple[int, dict]:
        mb_per_s = float(q.get("mb_per_s", "3"))

        def run() -> None:
            while True:
                hoard.append(bytearray(b"x") * int(mb_per_s * 1e6 / 4))  # writes every page
                time.sleep(0.25)

        threading.Thread(target=run, daemon=True).start()
        return 200, {"leaking_mb_per_s": mb_per_s}

    return {"/stock": stock, "/_ctl/leak": leak}


# ----- api-gateway: what users hit; turns upstream failures into 502s ---------------------------
def api_gateway(port: int) -> dict[str, Handler]:
    upstream = {"orders-service": os.environ["ORDERS_URL"], "inventory-service": os.environ["INVENTORY_URL"]}

    def call(name: str, path: str) -> None:
        try:
            get_json(upstream[name] + path, 3.0)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"upstream {name}: returned {e.code}") from None
        except (urllib.error.URLError, ConnectionError) as e:
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            if isinstance(reason, ConnectionRefusedError):
                raise RuntimeError(f"upstream {name}: connection refused ({upstream[name][7:]})") from None  # noqa: TRY004
            raise RuntimeError(f"upstream {name}: {reason}") from None
        except TimeoutError:
            raise RuntimeError(f"upstream {name}: timeout after 3.0s") from None

    def checkout(_: dict[str, str]) -> tuple[int, dict]:
        call("orders-service", "/order")
        call("inventory-service", "/stock")
        return 200, {"status": "ok"}

    return {"/checkout": checkout}


BUILDERS = {
    "orders-db": orders_db,
    "orders-service": orders_service,
    "inventory-service": inventory_service,
    "api-gateway": api_gateway,
}


def main() -> None:
    name, port = sys.argv[1], int(os.environ["PORT"])
    serve(name, port, BUILDERS[name](port))


if __name__ == "__main__":
    main()
