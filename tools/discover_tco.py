"""Dump TCeConnects TCO rows whose name suggests Cove Point.

Cove Point LNG is in Lusby, Calvert County, MD. Likely substrings:
  COVE, LUSBY, CALVERT, COVEPT, DOMINION, DCP

Run:  python tools/discover_tco.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ng_feedgas.scrapers.tceconnects import ASSETS  # noqa: E402

import pandas as pd  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> None:
    asset_id, asset_label, report_path = ASSETS["TCO"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.goto(f"https://ebb.tceconnects.com/infopost/Default.aspx?assetid={asset_id}",
                  wait_until="networkidle", timeout=60000)
        page.evaluate(f"changeAsset({asset_id!r}, {asset_label!r})")
        page.wait_for_load_state("networkidle", timeout=30000)
        report_url = (
            f"https://ebb.tceconnects.com/infopost/ReportViewer.aspx"
            f"?/InfoPost/{report_path}&pAssetNbr={asset_id}"
        )
        page.goto(report_url, wait_until="networkidle", timeout=120000)
        page.wait_for_timeout(5000)
        with page.expect_download(timeout=120000) as dl_info:
            page.evaluate("$find('ReportViewer1').exportReport('CSV')")
        dl = dl_info.value
        tmp = Path(tempfile.gettempdir()) / "tco_dump.csv"
        dl.save_as(str(tmp))
        df = pd.read_csv(tmp)
        browser.close()

    print("Columns:", list(df.columns))
    print(f"Rows: {len(df)}")
    print()

    needles = ["COVE", "LUSBY", "CALVERT", "DOMINION", "DCP", "BHE", "BERKSHIRE"]
    mask = df["LocationName"].astype(str).str.upper().apply(
        lambda s: any(n in s for n in needles)
    )
    hits = df[mask].copy()
    if hits.empty:
        print("No matches for any of:", needles)
        # Show top 30 rows by TSQ so we can eyeball
        print("\nTop 30 rows by TotalScheduledQuantity:")
        hits = df.sort_values("TotalScheduledQuantity", ascending=False).head(30)
    for _, r in hits.iterrows():
        print(f"  Loc={str(r.get('Location','')):>10}  Flow={str(r.get('FlowInd','')):>3}  "
              f"TSQ={str(r.get('TotalScheduledQuantity','')):>12}  "
              f"{r.get('LocationName','')}")


if __name__ == "__main__":
    main()
