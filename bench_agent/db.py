"""Load the benchmark CSVs into an in-memory SQLite database.  (GIVEN)

Tables:
  results(version, description, image_w, image_h, sigma, radius, time_ms, max_abs_error)
  findings(topic, finding)

Replace data/results.csv with your own full benchmark output (more sizes, radii,
fp16/u8 variants) whenever you like: the schema is read from the CSV header.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _coerce(value: str):
    if value == "":
        return None
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def load_csv(conn: sqlite3.Connection, table: str, path: Path) -> None:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{path} is empty")
    cols = list(rows[0].keys())
    conn.execute(f'CREATE TABLE "{table}" ({", ".join(f"{c!s}" for c in cols)})')
    conn.executemany(
        f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})',
        [[_coerce(r[c]) for c in cols] for r in rows],
    )


def connect(data_dir: Path = DATA_DIR) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    load_csv(conn, "results", data_dir / "results.csv")
    load_csv(conn, "findings", data_dir / "findings.csv")
    conn.commit()
    return conn


def schema(conn: sqlite3.Connection) -> str:
    """Human/LLM-readable description of the tables."""
    out = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        out.append(f"{name}({', '.join(cols)})")
    return "\n".join(out)
