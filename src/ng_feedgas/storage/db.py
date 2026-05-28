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
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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


def us_total(conn: sqlite3.Connection, gas_day: date, cycle: str) -> float:
    return sum(terminal_totals(conn, gas_day, cycle).values())


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
