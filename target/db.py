"""The real database: a SQLite file with an orders table and an index on the customer email."""

from __future__ import annotations

import sqlite3
from pathlib import Path

INDEX = "idx_orders_email"
QUERY = "SELECT id, total FROM orders WHERE customer_email = ?"


def ensure_db(path: Path, rows: int) -> Path:
    """Create the database once. Re-creating it only if it has a different number of rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        con = sqlite3.connect(path)
        try:
            if con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == rows:
                return path
        except sqlite3.DatabaseError:
            pass
        finally:
            con.close()
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_email TEXT, total REAL)")
    con.executemany(
        "INSERT INTO orders (customer_email, total) VALUES (?, ?)",
        ((f"user{i % 50000}@example.com", (i % 997) / 7) for i in range(rows)),
    )
    con.execute(f"CREATE INDEX {INDEX} ON orders (customer_email)")
    con.commit()
    con.close()
    return path
