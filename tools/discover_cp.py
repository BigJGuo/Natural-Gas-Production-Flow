"""Find Cove Point LNG delivery point on TETCO (Enbridge) and Transco (Williams).

Cove Point LNG is in Lusby, Calvert County, MD. Cove Point Pipeline itself is
private/Berkshire-owned (no public EBB), but two FERC interstates feed it:
  - TETCO (Texas Eastern, scrape via Enbridge) — Larger feeder
  - Transco (Williams) — Likely Loc near Pleasant Valley MD or M-3 zone

Run:  python tools/discover_cp.py
"""
from __future__ import annotations

import io
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from ng_feedgas.scrapers.enbridge import (  # noqa: E402
    EnbridgeTETCOScraper, FORM_URL, CYCLE_TO_TETCO_PREFIX,
    DROPDOWN_NAME, DOWNLOAD_TARGET,
)
from ng_feedgas.scrapers.williams import (  # noqa: E402
    WilliamsScraper, LANDING_URL, FORM_URL as W_FORM_URL,
    POST_URL, REPORT_URL, CYCLE_TO_WILLIAMS, parse_oac_report,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

NEEDLES = ["COVE", "LUSBY", "CALVERT", "PLEASANT VALLEY", "DOMINION", "DOM", "DCP", "BHE", "POINT"]
GAS_DAY = date.today() - timedelta(days=1)


def search_tetco() -> None:
    """Download TETCO and grep for Cove Point."""
    print("\n==== TETCO (Enbridge) — Cove Point candidates ====")
    scraper = EnbridgeTETCOScraper()
    scraper.session.headers.setdefault(
        "User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )
    r1 = scraper.session.get(FORM_URL, timeout=30)
    soup = BeautifulSoup(r1.text, "lxml")
    form = soup.find("form")
    form_data = {}
    for inp in form.find_all(["input", "select"]):
        name = inp.get("name")
        if not name or inp.get("type") in ("button", "submit", "image"):
            continue
        if inp.name == "select":
            sel = inp.find("option", selected=True) or inp.find("option")
            form_data[name] = sel.get("value", "") if sel else ""
        else:
            form_data[name] = inp.get("value", "")
    sel = form.find("select", {"name": DROPDOWN_NAME})
    opts = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.find_all("option") if o.get("value")]
    gas_day_token = GAS_DAY.strftime("%Y-%m-%d")
    chosen = None
    for val, _ in opts:
        if val.startswith("LATEC_") and gas_day_token in val:
            chosen = val
            break
    if not chosen:
        chosen = opts[0][0]
    form_data[DROPDOWN_NAME] = chosen
    form_data["ctl00$MainContent$ctl01$oaDefault$ucDate$rdpDate"] = GAS_DAY.strftime("%Y-%m-%d")
    form_data["ctl00$MainContent$ctl01$oaDefault$ucDate$rdpDate$dateInput"] = (
        f"{GAS_DAY.month}/{GAS_DAY.day}/{GAS_DAY.year}"
    )
    form_data["__EVENTTARGET"] = DOWNLOAD_TARGET
    form_data["__EVENTARGUMENT"] = ""

    r2 = scraper.session.post(FORM_URL, data=form_data, timeout=120)
    df = pd.read_csv(io.BytesIO(r2.content))
    print(f"  TETCO rows: {len(df)}")
    mask = df["Loc_Name"].astype(str).str.upper().apply(
        lambda s: any(n in s for n in NEEDLES)
    )
    hits = df[mask].copy()
    if hits.empty:
        print("  (no name matches; sample top-25 by TSQ)")
        df["_n"] = pd.to_numeric(df["Total_Scheduled_Quantity"].astype(str).str.replace(",", ""), errors="coerce").fillna(0)
        hits = df.sort_values("_n", ascending=False).head(25)
    for _, r in hits.iterrows():
        print(f"    Loc={str(r['Loc']):>8s}  Flow={str(r.get('Flow_Ind_Desc','')):<10s}  "
              f"TSQ={str(r.get('Total_Scheduled_Quantity','')):>14s}  {r['Loc_Name']}")


def search_transco() -> None:
    print("\n==== Transco (Williams) — Cove Point candidates ====")
    scraper = WilliamsScraper()
    scraper.session.headers.setdefault(
        "User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )
    scraper.session.get(LANDING_URL, timeout=30)
    scraper.session.headers["Referer"] = LANDING_URL
    scraper.session.get(W_FORM_URL, timeout=30)
    post_data = {
        "MapID": "0",
        "submitflag": "true",
        "tbGasFlowBeginDate": GAS_DAY.strftime("%m/%d/%Y"),
        "tbGasFlowEndDate": GAS_DAY.strftime("%m/%d/%Y"),
        "cycle": CYCLE_TO_WILLIAMS["evening"],
        "locationIDs": "",
        "reportType": "OAC",
    }
    scraper.session.headers["Referer"] = POST_URL
    scraper.session.post(POST_URL, data=post_data, timeout=60)
    r = scraper.session.get(REPORT_URL, timeout=120)
    rows = parse_oac_report(r.text)
    print(f"  Transco rows: {len(rows)}")
    hits = [r for r in rows if any(n in r["loc_name"].upper() for n in NEEDLES)]
    if not hits:
        print("  (no name matches; top-25 by TSQ)")
        for r in rows:
            try:
                r["_n"] = float(r["tsq"].replace(",", "") or 0)
            except ValueError:
                r["_n"] = 0
        hits = sorted(rows, key=lambda x: x["_n"], reverse=True)[:25]
    for r in hits:
        print(f"    Loc={r['loc_id']:>8s}  Flow={r['flow_ind']:<3s}  "
              f"TSQ={r['tsq']:>14s}  {r['loc_name']}")


if __name__ == "__main__":
    search_tetco()
    search_transco()
