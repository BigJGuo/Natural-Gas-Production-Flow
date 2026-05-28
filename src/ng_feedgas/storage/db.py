"""SQLite persistence for scraped flows."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from ..models import FlowRecord

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "feedgas.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets readers and a writer coexist; busy_timeout makes a blocked write
    # wait-and-retry instead of failing immediately with "database is locked".
    # Required because intraday-fast (5 min) and intraday-slow (15 min) tasks
    # write concurrently every 15 minutes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        _init(conn)
        yield conn
    finally:
        conn.close()


def upsert_flows(conn: sqlite3.Connection, records: Iterable[FlowRecord]) -> int:
    rows = [
        (
            r.gas_day.isoformat(),
            r.cycle,
            r.terminal,
            r.pipeline,
            r.meter_point,
            r.mmcfd,
            r.direction,
            r.scraped_at.isoformat(),
            r.source_url,
        )
        for r in records
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO flows
            (gas_day, cycle, terminal, pipeline, meter_point, mmcfd, direction, scraped_at, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (gas_day, cycle, pipeline, meter_point) DO UPDATE SET
            mmcfd       = excluded.mmcfd,
            direction   = excluded.direction,
            terminal    = excluded.terminal,
            scraped_at  = excluded.scraped_at,
            source_url  = excluded.source_url
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def flows_for_day(conn: sqlite3.Connection, gas_day: date, cycle: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM flows WHERE gas_day = ? AND cycle = ? ORDER BY terminal, pipeline",
        (gas_day.isoformat(), cycle),
    )
    return cur.fetchall()


def terminal_totals(
    conn: sqlite3.Connection, gas_day: date, cycle: str
) -> dict[str, float]:
    cur = conn.execute(
        """
        SELECT terminal, SUM(mmcfd) AS total
        FROM flows
        WHERE gas_day = ? AND cycle = ? AND direction = 'delivery'
        GROUP BY terminal
        """,
        (gas_day.isoformat(), cycle),
    )
    return {row["terminal"]: float(row["total"] or 0.0) for row in cur.fetchall()}


def terminal_totals_directional(
    conn: sqlite3.Connection, gas_day: date, cycle: str
) -> dict[str, float]:
    """Per-terminal totals counting the useful direction for each terminal.

    Export terminals (LNG, Mexico, Canada-export) count direction='delivery';
    Canada import terminals (Sumas, Waddington, Emerson) count 'receipt'. Use
    this for validation and reporting so imports aren't dropped (the plain
    `terminal_totals` is delivery-only and reports 0 for import points).
    """
    from ..config import CANADA_IMPORT_TERMINALS
    cur = conn.execute(
        """
        SELECT terminal, direction, SUM(mmcfd) AS total
        FROM flows
        WHERE gas_day = ? AND cycle = ?
        GROUP BY terminal, direction
        """,
        (gas_day.isoformat(), cycle),
    )
    out: dict[str, float] = {}
    for row in cur.fetchall():
        term = row["terminal"]
        want = "receipt" if term in CANADA_IMPORT_TERMINALS else "delivery"
        if row["direction"] == want:
            out[term] = out.get(term, 0.0) + float(row["total"] or 0.0)
    return out


def us_total(conn: sqlite3.Connection, gas_day: date, cycle: str) -> float:
    return sum(terminal_totals(conn, gas_day, cycle).values())


def terminal_series(
    conn: sqlite3.Connection,
    cycle: str,
    end_day: date,
    days: int,
) -> dict[str, dict[date, float]]:
    """Per-terminal daily totals over a window, for deviation analysis.

    Returns {terminal: {gas_day: total_mmcfd}} for the `days` ending at end_day.

    Unlike `terminal_totals`, this does NOT filter to direction='delivery' — it
    sums all rows per (terminal, gas_day). Each terminal's meters are
    consistently one direction (LNG/Mexico/Canada-export = delivery;
    Canada-import = receipt), so the per-terminal sum is the magnitude of flow
    at that point regardless of import/export.
    """
    start = end_day - timedelta(days=days - 1)
    cur = conn.execute(
        """
        SELECT gas_day, terminal, SUM(mmcfd) AS total
        FROM flows
        WHERE cycle = ? AND gas_day BETWEEN ? AND ?
        GROUP BY gas_day, terminal
        """,
        (cycle, start.isoformat(), end_day.isoformat()),
    )
    out: dict[str, dict[date, float]] = {}
    for row in cur.fetchall():
        gd = date.fromisoformat(row["gas_day"])
        out.setdefault(row["terminal"], {})[gd] = float(row["total"] or 0.0)
    return out


def us_totals_range(
    conn: sqlite3.Connection,
    end_day: date,
    cycle: str,
    days: int,
) -> dict[date, float]:
    """Returns {gas_day: us_total} for `days` ending at (and including) end_day."""
    start = end_day - timedelta(days=days - 1)
    cur = conn.execute(
        """
        SELECT gas_day, SUM(mmcfd) AS total
        FROM flows
        WHERE cycle = ? AND direction = 'delivery'
              AND gas_day BETWEEN ? AND ?
        GROUP BY gas_day
        ORDER BY gas_day
        """,
        (cycle, start.isoformat(), end_day.isoformat()),
    )
    return {date.fromisoformat(row["gas_day"]): float(row["total"] or 0.0) for row in cur.fetchall()}
