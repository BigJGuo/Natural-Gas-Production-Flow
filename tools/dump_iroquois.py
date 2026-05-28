"""Dump every row of the Iroquois OAC grid so we can pick the right meter IDs.

Scrolls the ExtJS grid to force all virtual rows to render, then dumps them.

Run:  python tools/dump_iroquois.py
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 1200},
        )
        page = ctx.new_page()
        page.goto("https://ioly.iroquois.com/infopost/",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)

        page.evaluate("""
            (() => {
                const tgt = [...document.querySelectorAll('.x-tree-node-text')]
                    .find(n => n.innerText.trim() === 'Capacity');
                if (tgt) tgt.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
            })()
        """)
        page.wait_for_timeout(2500)
        page.evaluate("""
            (() => {
                const tgt = [...document.querySelectorAll('.x-tree-node-text')]
                    .find(n => n.innerText.trim() === 'Operationally Available');
                if (tgt) tgt.click();
            })()
        """)
        page.wait_for_timeout(8000)

        # Click Retrieve
        page.evaluate("""
            (() => {
                const btn = [...document.querySelectorAll('a, button, span')]
                    .find(b => (b.innerText||'').trim() === 'Retrieve');
                if (btn) btn.click();
            })()
        """)
        page.wait_for_timeout(8000)

        # Scroll the grid to bottom to force all rows to render
        # ExtJS grids: scroll the .x-grid-view container
        for i in range(20):
            page.evaluate("""
                (() => {
                    const view = document.querySelector('.x-grid-view');
                    if (view) view.scrollTop += 800;
                })()
            """)
            page.wait_for_timeout(400)

        # Now scroll back to top + bottom a few times to collect all rendered rows
        all_rows: list[list[str]] = []
        seen_keys: set[tuple] = set()
        for direction in ("top", "bottom", "top", "bottom", "top"):
            page.evaluate(f"""
                (() => {{
                    const view = document.querySelector('.x-grid-view');
                    if (view) view.scrollTop = {0 if direction == 'top' else 999999};
                }})()
            """)
            page.wait_for_timeout(800)
            new_rows = page.evaluate("""
                (() => {
                    const rows = [...document.querySelectorAll('.x-grid-row')];
                    return rows.map(tr => [...tr.querySelectorAll('.x-grid-cell-inner')]
                        .map(c => (c.innerText || '').trim()));
                })()
            """)
            for r in new_rows:
                if not r or len(r) < 4:
                    continue
                key = tuple(r[:2])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_rows.append(r)

        print(f"Collected {len(all_rows)} unique rows (after scroll passes):")
        print()
        for r in all_rows:
            cells = r + [""] * (10 - len(r))
            print(f"  Loc={cells[0]:<8} Name={cells[1]:<40} Purp={cells[2]:<20} Flow={cells[3]:<10} TSQ={cells[6]}")

        browser.close()


if __name__ == "__main__":
    main()
