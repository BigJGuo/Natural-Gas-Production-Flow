"""Iroquois IOL probe v2 — drive ExtJS menu tree and capture all XHRs.

Strategy:
  1. Load page, let Imperva check pass
  2. Find Capacity tree node, expand it
  3. Click each child node, capture every XHR after each click
  4. Save the master XHR log

Run:  python tools/probe_iroquois2.py
"""
from __future__ import annotations

from pathlib import Path
from playwright.sync_api import sync_playwright


def main() -> None:
    log = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
        )
        page = ctx.new_page()

        def on_response(resp):
            url = resp.url
            if "ioly.iroquois.com" not in url:
                return
            if any(k in url for k in ("/infopost/")):
                log.append((resp.status, resp.headers.get("content-type", "")[:30], url))

        page.on("response", on_response)

        page.goto("https://ioly.iroquois.com/infopost/",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(6000)
        print(f">> Initial load complete. URL={page.url}")
        print(f">> Total XHRs observed so far: {len(log)}")

        # Dump everything Capacity-related from the page DOM (might reveal tree IDs)
        capacity_nodes = page.evaluate("""
            (() => {
                const out = [];
                document.querySelectorAll('*').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (!t) return;
                    if (t === 'Capacity' || t === 'Operationally Available Capacity'
                        || t.includes('Operationally Available')) {
                        out.push({
                            tag: el.tagName,
                            id: el.id || '',
                            cls: el.className ? String(el.className).slice(0,80) : '',
                            txt: t.slice(0,80),
                            outer: el.outerHTML.slice(0,200),
                        });
                    }
                });
                return out.slice(0, 20);
            })()
        """)
        print(">> Capacity-related DOM elements:")
        for n in capacity_nodes:
            print("   ", n)

        # Try clicking "Capacity" parent then expand
        print(">> Expanding 'Capacity' menu via parent click...")
        page.evaluate("""
            (() => {
                const els = [...document.querySelectorAll('*')].filter(e =>
                    (e.innerText||'').trim() === 'Capacity'
                );
                els.forEach(e => e.click());
            })()
        """)
        page.wait_for_timeout(3000)

        # Now look for OAC child link/node
        oac_nodes = page.evaluate("""
            (() => {
                const out = [];
                document.querySelectorAll('*').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t.includes('Operationally Available') && t.length < 80) {
                        out.push({
                            tag: el.tagName,
                            id: el.id || '',
                            cls: el.className ? String(el.className).slice(0,80) : '',
                            txt: t,
                        });
                    }
                });
                return out;
            })()
        """)
        print(">> 'Operationally Available' nodes after expand:")
        for n in oac_nodes:
            print("   ", n)

        # Click each one
        for n in oac_nodes[:3]:
            print(f">> Clicking node id={n.get('id')!r} txt={n.get('txt')!r}")
            page.evaluate(f"""
                (() => {{
                    const el = document.getElementById({n.get('id', '')!r}) ||
                        [...document.querySelectorAll('*')].find(e =>
                            (e.innerText||'').trim() === {n.get('txt', '')!r}
                        );
                    if (el) el.click();
                }})()
            """)
            page.wait_for_timeout(5000)
            print(f"   URL after click: {page.url}")

        print()
        print(f">> Final XHR log ({len(log)} entries):")
        # Dedupe by URL
        seen = set()
        for s, ct, u in log:
            key = u.split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            print(f"   {s:>3}  {ct:<30}  {u[:150]}")

        # Save full HTML at end for debugging
        Path("/tmp/iro_final.html").write_text(page.content(), encoding="utf-8", errors="ignore")
        print(">> Saved final DOM to /tmp/iro_final.html")
        browser.close()


if __name__ == "__main__":
    main()
