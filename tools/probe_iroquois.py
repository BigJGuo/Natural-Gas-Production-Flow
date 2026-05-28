"""Probe Iroquois Gas Transmission System (IOL) via Playwright.

Site is a Sencha ExtJS SPA behind Imperva/Incapsula bot protection. Bare HTTP
gets 403; a real Chromium browser should pass the bot check.

Goals:
  1. Confirm we can reach the post-Imperva content.
  2. Capture any XHR endpoints the SPA calls — that's the path to a non-Playwright
     scraper later.
  3. If we can navigate to an OAC report, dump the location rows so we can find
     Waddington (the Canadian-border receipt point).

Run:  python tools/probe_iroquois.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    xhrs: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        page = ctx.new_page()

        def on_response(resp):
            url = resp.url
            if "infopost" in url and any(k in url for k in (
                "/api", "/data", "/services", "ashx", "aspx", "json",
                "Capacity", "OAC", "OperationallyAvailable", "Location",
            )):
                try:
                    body_preview = resp.body()[:200]
                except Exception:
                    body_preview = b""
                xhrs.append({
                    "url": url,
                    "status": resp.status,
                    "ct": resp.headers.get("content-type", ""),
                    "preview": body_preview.decode("utf-8", errors="replace"),
                })

        page.on("response", on_response)

        print(">> goto https://ioly.iroquois.com/infopost/", flush=True)
        page.goto("https://ioly.iroquois.com/infopost/",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(8000)

        print(">> page title:", page.title())

        # Click on "Capacity" menu item
        print(">> clicking Capacity menu...")
        page.evaluate("""
            const els = [...document.querySelectorAll('a, span, div')];
            const cap = els.find(e => (e.innerText||'').trim() === 'Capacity');
            if (cap) cap.click();
        """)
        page.wait_for_timeout(3000)

        # Look for OAC-related submenu items
        for needle in ("Operationally Available", "OAC"):
            print(f">> clicking '{needle}'...")
            clicked = page.evaluate(f"""
                (() => {{
                    const els = [...document.querySelectorAll('a, span, div')];
                    const target = els.find(e => (e.innerText||'').includes({needle!r}));
                    if (target) {{ target.click(); return target.outerHTML.slice(0, 200); }}
                    return null;
                }})()
            """)
            if clicked:
                print(f"   clicked: {clicked[:200]}")
                page.wait_for_timeout(5000)
                break

        print(">> URL now:", page.url)
        body2 = page.evaluate("document.body.innerText")[:1500]
        print(">> page body sample (after navigation):")
        print("---")
        print(body2)
        print("---")

        print()
        print(f">> Captured {len(xhrs)} interesting XHR responses:")
        for x in xhrs[:20]:
            print(f"   {x['status']:>3}  {x['ct'][:30]:<30}  {x['url']}")
            if x["preview"].strip():
                print(f"        preview: {x['preview'][:150]!r}")

        browser.close()


if __name__ == "__main__":
    main()
