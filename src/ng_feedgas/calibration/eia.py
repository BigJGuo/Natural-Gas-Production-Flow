"""EIA monthly LNG export data — used as a calibration baseline.

EIA's Open Data v2 API requires a free API key (register at
https://www.eia.gov/opendata/register.php). Once you have it, set the env var:

    setx EIA_API_KEY "your-key-here"     # Windows
    export EIA_API_KEY="your-key-here"   # macOS/Linux

The function `fetch_weekly_lng_exports(n_weeks)` (kept under its historical name
for backwards compatibility) returns a pandas DataFrame with one row per MONTH:
  - period: month-end date (last day of the month)
  - us_lng_bcfd: U.S. LNG exports in Bcf/d (monthly total averaged over days)
  - source_url: the API URL used

NOTE on weekly vs monthly (verified 2026-05-27): EIA's legacy weekly LNG series
NG.N9133US2.W was retired with the v2 API migration; only the monthly series
N9133US2 (`natural-gas/move/poe2`, process=ENG) remains. The function name still
says 'weekly' for compat with the existing storage table; the data is monthly.

Without an API key, this module raises ValueError — there's no clean public
no-auth source for the same data.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

EIA_API_BASE = "https://api.eia.gov/v2"
DEFAULT_API_KEY_ENV = "EIA_API_KEY"


def get_api_key() -> str | None:
    return os.environ.get(DEFAULT_API_KEY_ENV)


def fetch_weekly_lng_exports(n_weeks: int = 52, api_key: str | None = None) -> pd.DataFrame:
    """Fetch the last n_weeks months of EIA monthly LNG export totals.

    Returns a DataFrame with columns ['period', 'us_lng_bcfd', 'source_url'].
    Despite the historical 'weekly' name, this now returns monthly data — the
    EIA v2 weekly LNG series was retired. n_weeks is used as the month count.

    Raises ValueError if no API key is configured.
    """
    import calendar
    from datetime import date

    key = api_key or get_api_key()
    if not key:
        raise ValueError(
            f"No EIA API key. Get one free at https://www.eia.gov/opendata/register.php "
            f"and set the {DEFAULT_API_KEY_ENV!r} env var."
        )

    # EIA v2 monthly LNG exports — natural-gas/move/poe2, process=ENG (LNG Exports)
    # Returns volume in MMcf for the month. We convert to Bcf/d (avg over days
    # in month) so the column matches the historical 'us_lng_bcfd' semantic.
    url = f"{EIA_API_BASE}/natural-gas/move/poe2/data/"
    params = [
        ("api_key", key),
        ("frequency", "monthly"),
        ("data[0]", "value"),
        ("facets[duoarea][]", "NUS-Z00"),   # U.S. → World
        ("facets[process][]", "ENG"),        # Liquefied Natural Gas Exports
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", str(max(1, min(n_weeks, 360)))),
    ]
    log.info("EIA: fetching monthly LNG exports (n=%d months)", n_weeks)
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if "response" not in payload or "data" not in payload["response"]:
        raise ValueError(f"EIA: unexpected response shape: {list(payload.keys())}")
    rows = payload["response"]["data"]
    if not rows:
        raise ValueError("EIA: zero rows returned for monthly LNG export series")

    df = pd.DataFrame(rows)
    df["mmcf_per_month"] = pd.to_numeric(df["value"], errors="coerce")

    def _period_to_month_end(s: str) -> date:
        y, m = map(int, s.split("-"))
        return date(y, m, calendar.monthrange(y, m)[1])

    df["period"] = df["period"].astype(str).map(_period_to_month_end)
    df["days_in_month"] = df["period"].map(lambda d: calendar.monthrange(d.year, d.month)[1])
    # MMcf for the whole month → Bcf/d avg (divide by 1000 for Bcf, then by days)
    df["us_lng_bcfd"] = df["mmcf_per_month"] / 1000.0 / df["days_in_month"]

    df = df[["period", "us_lng_bcfd"]].dropna().sort_values("period")
    df["source_url"] = r.url
    return df.reset_index(drop=True)


def _fetch_via_seriesid(api_key: str, series_id: str, n: int) -> pd.DataFrame:
    """Fallback path using the legacy series-id endpoint (also in v2)."""
    url = f"{EIA_API_BASE}/seriesid/{series_id}"
    params = {"api_key": api_key, "length": n, "sort[0][column]": "period", "sort[0][direction]": "desc"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    rows = r.json()["response"]["data"]
    df = pd.DataFrame(rows)
    df["us_lng_bcfd"] = pd.to_numeric(df["value"], errors="coerce")
    df["period"] = pd.to_datetime(df["period"]).dt.date
    return df[["period", "us_lng_bcfd"]].dropna().sort_values("period").reset_index(drop=True)


def upsert_to_db(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Persist EIA weekly rows to the eia_weekly table."""
    if df.empty:
        return 0
    now = datetime.utcnow().isoformat()
    rows = [
        (row["period"].isoformat(), float(row["us_lng_bcfd"]),
         row.get("source_url", ""), now)
        for _, row in df.iterrows()
    ]
    conn.executemany(
        """
        INSERT INTO eia_weekly (week_ending, us_lng_bcfd, source_url, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(week_ending) DO UPDATE SET
            us_lng_bcfd = excluded.us_lng_bcfd,
            source_url  = excluded.source_url,
            fetched_at  = excluded.fetched_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_from_db(conn: sqlite3.Connection) -> pd.DataFrame:
    """Read previously-fetched EIA weekly data from the local DB."""
    try:
        df = pd.read_sql_query(
            "SELECT week_ending, us_lng_bcfd, source_url, fetched_at FROM eia_weekly ORDER BY week_ending",
            conn,
        )
    except Exception:
        return pd.DataFrame(columns=["week_ending", "us_lng_bcfd", "source_url", "fetched_at"])
    df["week_ending"] = pd.to_datetime(df["week_ending"]).dt.date
    return df
