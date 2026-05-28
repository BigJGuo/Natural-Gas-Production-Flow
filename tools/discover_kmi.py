"""Probe KMI portal codes and dump candidate border/LNG meter points.

For each pipeline code below, downloads the operationally available capacity
.xlsx for yesterday's evening cycle and prints rows whose Loc Name contains
border or LNG keywords. Use to verify meter IDs for new YAML entries.

Run:  python tools/discover_kmi.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ng_feedgas.models import Cycle  # noqa: E402
from ng_feedgas.scrapers.base import ScrapeContext  # noqa: E402
from ng_feedgas.scrapers.kmi import KMIScraper, _read_xlsx  # noqa: E402

# Codes confirmed to exist on the KMI portal (per portal landing fetch).
# Codes seen in pipeline2.kindermorgan.com landing page HTML
CANDIDATE_CODES = [
    "CP",     # Cove Point (FERC interstate Dominion)
    "SNG",    # Southern Natural Gas (try without D)
    "EPNG",   # El Paso Natural Gas (try without D)
]

# Substrings used to surface candidate LNG / Mexico-border / Canada-border rows.
# (Empty list means "show top rows by TSQ" so we can survey small assets.)
NEEDLES: list[str] = []


def main() -> None:
    gas_day = date.today() - timedelta(days=1)
    cycle: Cycle = "evening"
    ctx = ScrapeContext(gas_day=gas_day, cycle=cycle, meter_points=[])

    for code in CANDIDATE_CODES:
        print(f"\n==== {code} on {gas_day} {cycle} ====", flush=True)
        scraper = KMIScraper()  # FRESH session per code (KMI session is sticky)
        try:
            xls_bytes, url = scraper._download_xlsx(ctx, code)
        except Exception as exc:
            print(f"  ! download failed: {exc}")
            continue
        try:
            df = _read_xlsx(xls_bytes)
        except Exception as exc:
            print(f"  ! parse failed: {exc}")
            continue

        print(f"  rows: {len(df)}; columns: {list(df.columns)}")

        if NEEDLES:
            mask = df["Loc Name"].astype(str).str.upper().apply(
                lambda s: any(n in s for n in NEEDLES)
            )
            candidates = df[mask][
                ["Loc", "Loc Name", "Flow Ind", "Total Scheduled Quantity"]
            ].copy()
        else:
            candidates = df[
                ["Loc", "Loc Name", "Flow Ind", "Total Scheduled Quantity"]
            ].copy()
        if candidates.empty:
            print("  (no rows)")
            continue

        try:
            candidates["TSQ_num"] = (
                candidates["Total Scheduled Quantity"]
                .astype(str).str.replace(",", "").astype(float)
            )
            candidates = candidates.sort_values("TSQ_num", ascending=False)
        except Exception:
            pass

        for _, r in candidates.head(40).iterrows():
            tsq = r.get("Total Scheduled Quantity", "")
            print(f"    Loc={str(r['Loc']):>10s}  Flow={str(r.get('Flow Ind','')):>3s}  "
                  f"TSQ={str(tsq):>14s}  {r['Loc Name']}")


if __name__ == "__main__":
    main()
