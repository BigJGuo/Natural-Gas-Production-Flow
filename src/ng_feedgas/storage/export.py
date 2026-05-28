"""Emit the Part-4 entry table as CSV and human-readable text."""
from __future__ import annotations

import csv
import sqlite3
from datetime import date
from io import StringIO
from pathlib import Path

from ..config import load_config
from .db import flows_for_day, terminal_totals

# LNG terminal ordering, derived from the YAML config (single source of truth)
# so it can't drift out of sync with meter_points.yaml. Part-4/Part-5 cover U.S.
# LNG feedgas; Mexico/Canada cross-border points are reported separately.
TERMINAL_ORDER = load_config().lng_terminals()


def write_part4_csv(
    conn: sqlite3.Connection,
    gas_day: date,
    cycle: str,
    out_path: Path,
) -> Path:
    """Write a CSV mirroring the Part-4 template format."""
    flows = flows_for_day(conn, gas_day, cycle)
    totals = terminal_totals(conn, gas_day, cycle)

    flows_by_terminal: dict[str, list[sqlite3.Row]] = {}
    for row in flows:
        flows_by_terminal.setdefault(row["terminal"], []).append(row)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"Gas Day: {gas_day.isoformat()}", f"Cycle: {cycle}"])
        w.writerow(["Terminal", "Pipeline", "Meter Point", "MMcf/d", "Direction"])
        us_total = 0.0
        for terminal in TERMINAL_ORDER:
            rows = flows_by_terminal.get(terminal, [])
            for r in rows:
                w.writerow([
                    terminal,
                    r["pipeline"],
                    r["meter_point"],
                    f"{r['mmcfd']:.1f}",
                    r["direction"],
                ])
            total = totals.get(terminal, 0.0)
            us_total += total
            w.writerow(["", f"{terminal.upper()} TOTAL", "", f"{total:.1f}", ""])
            w.writerow([])
        w.writerow(["", "U.S. TOTAL FEEDGAS", "", f"{us_total:.1f}", ""])
    return out_path


def render_part4_text(
    conn: sqlite3.Connection,
    gas_day: date,
    cycle: str,
) -> str:
    """Return the Part-4 entry table as a plaintext block."""
    flows = flows_for_day(conn, gas_day, cycle)
    totals = terminal_totals(conn, gas_day, cycle)
    flows_by_terminal: dict[str, list[sqlite3.Row]] = {}
    for row in flows:
        flows_by_terminal.setdefault(row["terminal"], []).append(row)

    buf = StringIO()
    buf.write(f"Gas Day: {gas_day.isoformat()}     Cycle: {cycle}\n\n")
    fmt = "{:<24} | {:<22} | {:<22} | {:>8}\n"
    buf.write(fmt.format("TERMINAL", "PIPELINE", "METER POINT", "MMcf/d"))
    buf.write("-" * 90 + "\n")
    us_total = 0.0
    for terminal in TERMINAL_ORDER:
        rows = flows_by_terminal.get(terminal, [])
        for r in rows:
            buf.write(fmt.format(
                terminal[:24],
                str(r["pipeline"])[:22],
                str(r["meter_point"])[:22],
                f"{r['mmcfd']:.1f}",
            ))
        total = totals.get(terminal, 0.0)
        us_total += total
        buf.write(fmt.format("", f"{terminal.upper()} TOTAL"[:22], "", f"{total:.1f}"))
        buf.write("-" * 90 + "\n")
    buf.write(fmt.format("", "U.S. TOTAL FEEDGAS", "", f"{us_total:.1f}"))
    return buf.getvalue()
