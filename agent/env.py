"""Tiny .env loader so settings can live in a file instead of the terminal. No dependency."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Read KEY=VALUE lines into os.environ. Real environment variables win. Returns keys set."""
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Be forgiving: accept pasted shell syntax such as `$env:KEY="v"` or `export KEY=v`.
        key = key.strip().removeprefix("export ").removeprefix("$env:").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
