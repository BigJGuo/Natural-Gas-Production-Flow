"""Roll-up statistics for the daily report."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..storage.db import terminal_totals, us_totals_range


@dataclass(frozen=True)
class DailyStats:
    gas_day: date
    cycle: str
    terminal_totals: dict[str, float]
    us_total: float
    prior_day_us_total: float | None
    seven_day_avg: float | None
    thirty_day_avg: float | None


def compute(conn: sqlite3.Connection, gas_day: date, cycle: str) -> DailyStats:
    today_totals = terminal_totals(conn, gas_day, cycle)
    us_today = sum(today_totals.values())

    prior = us_totals_range(conn, gas_day - timedelta(days=1), cycle, days=1)
    prior_val = next(iter(prior.values()), None) if prior else None

    seven = us_totals_range(conn, gas_day, cycle, days=7)
    seven_avg = sum(seven.values()) / len(seven) if seven else None

    thirty = us_totals_range(conn, gas_day, cycle, days=30)
    thirty_avg = sum(thirty.values()) / len(thirty) if thirty else None

    return DailyStats(
        gas_day=gas_day,
        cycle=cycle,
        terminal_totals=today_totals,
        us_total=us_today,
        prior_day_us_total=prior_val,
        seven_day_avg=seven_avg,
        thirty_day_avg=thirty_avg,
    )
