"""Steady user traffic. Standard library only, so the stack image can run it without the rest of the repo."""

from __future__ import annotations

import random
import threading
import time
import urllib.request


class Load:
    """Steady user traffic against the gateway, from a few threads."""

    def __init__(self, url: str, threads: int = 4):
        self.url, self._stop = url, threading.Event()
        self._started = False
        self._threads = [threading.Thread(target=self._run, args=(i,), daemon=True) for i in range(threads)]

    def _run(self, seed: int) -> None:
        rng = random.Random(seed)
        while not self._stop.is_set():
            try:
                urllib.request.urlopen(self.url + "/checkout", timeout=4).read()
            except OSError:
                pass  # failures are the point; the services record them
            time.sleep(rng.uniform(0.08, 0.16))

    def start(self) -> None:
        self._started = True
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        """Safe to call more than once, and before start() (a surge that never began)."""
        self._stop.set()
        if self._started:
            for t in self._threads:
                t.join(timeout=2)
