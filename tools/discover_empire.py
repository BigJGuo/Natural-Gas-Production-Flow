"""Discovery probe: Empire Pipeline (National Fuel) — PeopleSoft OAC (Playwright).

Public (no-login) PeopleSoft component:
  https://sbsprd2.natfuel.com/psc/sbsprd/NFSBS/SBSPRD/c/NFOM_INFORMATIONAL_POSTINGS.NFOC_OPER_AVAIL_1.GBL

Goal: determine whether the OAC grid renders with data on load (parseable HTML),
and whether the CSV download link fires a capturable browser download. Reports
whether Chippawa appears and what the CSV buttons produce.

Run: python tools/discover_empire.py
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright

URL = ("https://sbsprd2.natfuel.com/psc/sbsprd/NFSBS/SBSPRD/c/"
       "NFOM_INFORMATIONAL_POSTINGS.NFOC_OPER_AVAIL_1.GBL")


def safe(x: str) -> str:
    return (x or "").encode("ascii", "replace").decode("ascii")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(accept_downloads=True)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)

        body = page.inner_text("body")
        print("Chippawa present on load:", "chippawa" in body.lower())
        print("rows w/ 'receipt'/'delivery':",
              sum(1 for ln in body.splitlines() if "receipt" in ln.lower() or "delivery" in ln.lower()))
        # Dump lines that look like location data rows
        print("=== sample lines mentioning border/location ===")
        for ln in body.splitlines():
            low = ln.lower()
            if any(k in low for k in ["chippawa", "border", "tcpl", "interconnect", "receipt", "scheduled"]):
                print("   ", safe(ln)[:150])

        # Capture the Evening CSV (button $0) and parse it.
        import io
        import re
        import pandas as pd
        try:
            with page.expect_download(timeout=10000) as dl_info:
                page.locator("[id='NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0']").click(timeout=6000)
            dl = dl_info.value
            content = open(dl.path(), encoding="utf-8", errors="replace").read()
            print(f"\n=== CSV$0 download: name={dl.suggested_filename} size={len(content)}")
            lines = content.splitlines()
            hdr_i = next((i for i, l in enumerate(lines)
                          if re.search(r"loc", l, re.I) and l.count(",") >= 3), None)
            print("header row idx:", hdr_i, "->", safe(lines[hdr_i])[:160] if hdr_i is not None else None)
            df = pd.read_csv(io.StringIO(content), skiprows=hdr_i)
            df.columns = [c.strip() for c in df.columns]
            print("COLS:", list(df.columns))
            print("nrows:", len(df))
            namecol = next((c for c in df.columns if "loc name" in c.lower()
                            or "location name" in c.lower()), df.columns[0])
            print("namecol:", namecol)
            for _, r in df.iterrows():
                nm = str(r.get(namecol, ""))
                if re.search(r"chippawa|transcanada|trans canada|\btc\b|border|canada|niagara|empire", nm, re.I):
                    print("   BORDER ROW:", {k: safe(str(r[k]))[:20] for k in df.columns[:7]})
        except Exception as exc:
            print("CSV$0 parse failed:", safe(str(exc))[:120])

        browser.close()


if __name__ == "__main__":
    main()
