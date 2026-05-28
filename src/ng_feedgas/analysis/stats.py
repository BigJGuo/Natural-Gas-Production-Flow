"""Roll-up statistics for the daily LNG report.

Scope note: Part-4/Part-5 cover U.S. LNG feedgas. All totals here are restricted
to LNG terminals (config.lng_terminals()) so the per-terminal lines and the
headline total are always consistent — Mexico exports and Canada border flows
are reported separately (and shown on the dashboard), never silently folded into
the LNG total. This is the fix for the Part-5 "lines sum to X but TOTAL prints Y"
inconsistency.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import load_config
from ..storage.db import terminal_totals, terminal_series


@dataclass(frozen=True)
class DailyStats:
    gas_day: date
    cycle: str
    terminal_totals: dict[str, float]   # LNG terminals only
    us_total: float                     # = sum(terminal_totals) — LNG only
    prior_day_us_total: float | None
    seven_day_avg: float | None
    thirty_day_avg: float | None


def compute(conn: sqlite3.Connection, gas_day: date, cycle: str) -> DailyStats:
    lng = load_config().lng_terminals()
    lng_set = set(lng)

    all_today = terminal_totals(conn, gas_day, cycle)   # delivery sums per terminal
    lng_today = {t: all_today.get(t, 0.0) for t in lng}
    us_today = sum(lng_today.values())

    # Per-day LNG totals from a 30-day window (terminal_series sums each terminal;
    # LNG terminals are all delivery-side so this equals their delivery total).
    series = terminal_series(conn, cycle, gas_day, days=30)

    def lng_total_on(day: date) -> float | None:
        present = any(day in series.get(t, {}) for t in lng_set)
        if not present:
            return None
        return sum(series.get(t, {}).get(day, 0.0) for t in lng_set)

    prior_val = lng_total_on(gas_day - timedelta(days=1))

    seven_vals = [v for i in range(7)
                  if (v := lng_total_on(gas_day - timedelta(days=i))) is not None]
    seven_avg = sum(seven_vals) / len(seven_vals) if seven_vals else None

    thirty_vals = [v for i in range(30)
                   if (v := lng_total_on(gas_day - timedelta(days=i))) is not None]
    thirty_avg = sum(thirty_vals) / len(thirty_vals) if thirty_vals else None

    return DailyStats(
        gas_day=gas_day,
        cycle=cycle,
        terminal_totals=lng_today,
        us_total=us_today,
        prior_day_us_total=prior_val,
        seven_day_avg=seven_avg,
        thirty_day_avg=thirty_avg,
    )
