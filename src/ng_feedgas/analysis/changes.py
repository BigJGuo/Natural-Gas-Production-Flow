"""Deviation tracking: day-over-day, week-over-week, month-over-month.

For each terminal, compares the latest gas day's flow against three horizons:
  - DoD  (day-over-day):   latest vs the prior gas day (point-to-point)
  - WoW  (week-over-week):  trailing 7-day mean vs the preceding 7-day mean
  - MoM  (month-over-month): trailing 30-day mean vs the preceding 30-day mean

Averages (not single points) are used for WoW/MoM to smooth cycle-to-cycle
noise. Each comparison degrades gracefully when there isn't enough history yet:
a horizon with no comparison data returns delta=None ("n/a — need more history").

The point of this module: LNG terminal absolute levels are lower bounds (see the
confidence tiers), but the *change* in captured flow is a real signal — a drop
usually means a train trip, maintenance, or curtailment. This surfaces those.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..storage.db import terminal_series

# Percent-change thresholds for severity flags.
NOTABLE_PCT = 10.0      # amber
SIGNIFICANT_PCT = 25.0  # red


@dataclass(frozen=True)
class Horizon:
    label: str            # "DoD" | "WoW" | "MoM"
    current: float | None
    baseline: float | None
    abs_delta: float | None
    pct_delta: float | None
    note: str = ""        # e.g. "need 5 more days"

    @property
    def severity(self) -> str:
        if self.pct_delta is None:
            return "none"
        p = abs(self.pct_delta)
        if p >= SIGNIFICANT_PCT:
            return "significant"
        if p >= NOTABLE_PCT:
            return "notable"
        return "ok"


@dataclass(frozen=True)
class TerminalChange:
    terminal: str
    current: float | None
    dod: Horizon
    wow: Horizon
    mom: Horizon

    @property
    def max_severity(self) -> str:
        order = {"significant": 3, "notable": 2, "ok": 1, "none": 0}
        worst = max((self.dod, self.wow, self.mom), key=lambda h: order[h.severity])
        return worst.severity


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _window_mean(series: dict[date, float], end: date, days: int) -> tuple[float | None, int]:
    """Mean of available daily values in [end-days+1, end]; returns (mean, n_present)."""
    vals = []
    for i in range(days):
        v = series.get(end - timedelta(days=i))
        if v is not None:
            vals.append(v)
    return _mean(vals), len(vals)


def _pct(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or base == 0:
        return None
    return (cur - base) / base * 100.0


def _horizon_point(label: str, series: dict[date, float], gas_day: date,
                   lag_days: int) -> Horizon:
    cur = series.get(gas_day)
    base = series.get(gas_day - timedelta(days=lag_days))
    if cur is None:
        return Horizon(label, None, base, None, None, note="no current-day data")
    if base is None:
        return Horizon(label, cur, None, None, None,
                       note=f"no data {lag_days}d ago")
    return Horizon(label, cur, base, cur - base, _pct(cur, base))


def _horizon_window(label: str, series: dict[date, float], gas_day: date,
                    window: int) -> Horizon:
    cur_mean, n_cur = _window_mean(series, gas_day, window)
    base_mean, n_base = _window_mean(series, gas_day - timedelta(days=window), window)
    if cur_mean is None:
        return Horizon(label, None, base_mean, None, None, note="no recent data")
    if base_mean is None:
        need = window  # rough: need a full prior window
        return Horizon(label, cur_mean, None, None, None,
                       note=f"need ~{need}d more history")
    return Horizon(label, cur_mean, base_mean, cur_mean - base_mean,
                   _pct(cur_mean, base_mean))


def compute_changes(conn: sqlite3.Connection, gas_day: date, cycle: str) -> list[TerminalChange]:
    """Per-terminal DoD/WoW/MoM deviations for the given gas day + cycle."""
    # Pull ~61 days so the MoM trailing/preceding 30-day windows both have room.
    series = terminal_series(conn, cycle, gas_day, days=61)
    out: list[TerminalChange] = []
    for terminal, s in series.items():
        out.append(TerminalChange(
            terminal=terminal,
            current=s.get(gas_day),
            dod=_horizon_point("DoD", s, gas_day, lag_days=1),
            wow=_horizon_window("WoW", s, gas_day, window=7),
            mom=_horizon_window("MoM", s, gas_day, window=30),
        ))
    # Sort: biggest movers first (by worst-horizon absolute pct), then name.
    def _sort_key(tc: TerminalChange):
        pcts = [abs(h.pct_delta) for h in (tc.dod, tc.wow, tc.mom)
                if h.pct_delta is not None]
        return (-(max(pcts) if pcts else -1), tc.terminal)
    out.sort(key=_sort_key)
    return out
