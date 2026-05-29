"""Discovery probe: Viking Gas Transmission (DT Midstream Trellis PTM).

Public infopost lives at dtmidstream.trellisenergy.com/ptms/home/infopost/VGT.
The report data is served by .do endpoints (no login):
  /ptms/public/infopost/viewInfoPostingReportTable.do?reportNumber=...&...

Goal: find the Operationally Available Capacity report endpoint + the EMERSON
receipt location ID. Captures all /infopost/ network responses, clicks through
the Capacity menu, and dumps candidate rows.

Run: python tools/discover_viking.py
"""
from __future__ import annotations

import re
from playwright.sync_api import sync_playwright

ENTRY = "https://dtmidstream.trellisenergy.com/ptms/home/infopost/VGT"


def main() -> None:
    captured: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_resp(resp):
            u = resp.url
            if "infopost" in u.lower() and (".do" in u or "Report" in u):
                captured.append(u)

        def on_resp2(resp):
            if ".do" in resp.url and "infopost" in resp.url.lower():
                captured.append(resp.url)

        page.on("response", on_resp2)
        page.goto(ENTRY, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        def safe(x: str) -> str:
            return (x or "").encode("ascii", "replace").decode("ascii")

        # Expand the "Capacity" tree node so its sub-items render into the DOM.
        for label in ["Capacity"]:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=5000)
                page.wait_for_timeout(2000)
                print(f"   expanded: {label!r}")
            except Exception as exc:
                print(f"   (expand {label!r} failed: {safe(str(exc))[:70]})")

        links = page.eval_on_selector_all(
            "a, span, li",
            "els => els.map(e => ({t:(e.textContent||'').trim(), "
            "h:(e.getAttribute('href')||e.getAttribute('onclick')||'')}))",
        )
        print("=== nodes mentioning operational/capacity/report (post-expand) ===")
        seen = set()
        for l in links:
            blob = (l["t"] + " " + l["h"]).lower()
            if re.search(r"operational|unsubscribed|reportnumber|viewinfoposting", blob):
                key = (l["t"][:40], l["h"][:80])
                if key not in seen and l["t"].strip():
                    seen.add(key)
                    print(f"   {safe(l['t'])[:46]:46s} {safe(l['h'])[:90]}")

        # Click "Operationally Available Capacity".
        for label in ["Operationally Available Capacity", "Operationally Available"]:
            try:
                page.get_by_text(label, exact=False).first.click(timeout=5000)
                page.wait_for_timeout(3500)
                print(f"   clicked: {label!r}")
                break
            except Exception as exc:
                print(f"   (click {label!r} failed: {safe(str(exc))[:70]})")

        page.wait_for_timeout(1500)

        # The OAC list shows posting instances (gas day + cycle). Click the most
        # recent row's link to open the location-level data report.
        try:
            row_link = page.eval_on_selector_all(
                "a",
                "els => els.map(e => ({t:(e.textContent||'').trim(), "
                "h:(e.getAttribute('href')||e.getAttribute('onclick')||'')}))"
                ".filter(x => /viewInfoPostingReport|postingId|reportInstance|view\\b/i.test(x.h))",
            )
            print("=== candidate data-report links ===")
            for l in row_link[:8]:
                print(f"   {safe(l['t'])[:30]:30s} {safe(l['h'])[:110]}")
            # click the first posting row (the date cell is usually the link)
            page.locator("table a").first.click(timeout=5000)
            page.wait_for_timeout(3500)
        except Exception as exc:
            print("row click failed:", safe(str(exc))[:90])

        # Dump any download/export links + the location table with Emerson.
        try:
            dl = page.eval_on_selector_all(
                "a",
                "els => els.map(e => (e.getAttribute('href')||e.getAttribute('onclick')||''))"
                ".filter(h => /csv|excel|export|download|\\.do/i.test(h))",
            )
            print("=== export/.do links on data view ===")
            for h in sorted(set(dl))[:15]:
                print("   ", safe(h)[:140])
            print("PAGE URL after row click:", safe(page.url)[:140])
            # Find the widest table (the location grid) and dump receipt rows + names.
            big = page.eval_on_selector_all(
                "table",
                "ts => { let best=''; for (const t of ts){ if((t.innerText||'').length>best.length) best=t.innerText;} return best; }",
            )
            big = big[0] if big else ""
            rows = [r for r in big.splitlines() if r.strip()]
            print(f"=== widest table: {len(rows)} non-empty lines; first 3 ===")
            for ln in rows[:3]:
                print("   ", safe(ln)[:150])
            print("=== rows matching border/receipt keywords ===")
            for ln in rows:
                low = ln.lower()
                if any(k in low for k in ["emerson", "noyes", "manitoba", "tcpl", "border", "receipt", "interconnect"]):
                    print("   ", safe(ln)[:170])
        except Exception as exc:
            print("data dump failed:", safe(str(exc))[:90])

        browser.close()

    print("\n=== captured /infopost/ report endpoints ===")
    for u in sorted(set(captured)):
        print("  ", u[:160])


if __name__ == "__main__":
    main()
