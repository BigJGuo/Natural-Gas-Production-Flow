"""Iroquois IOL probe v3 — expand entire menu tree, capture every RouterClass.php call.

This time we double-click each tree node (ExtJS tree default expand action)
and use Playwright's request interception to capture each POST/GET body.

Run:  python tools/probe_iroquois3.py
"""
from __future__ import annotations

import base64

from playwright.sync_api import sync_playwright


def b64d(s: str) -> str:
    try:
        return base64.b64decode(s).decode("utf-8", errors="replace")
    except Exception:
        return s


def main() -> None:
    router_calls: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
        )
        page = ctx.new_page()

        def on_response(resp):
            if "RouterClass.php" in resp.url:
                # Parse class/type from query string
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(resp.url).query)
                cls_b = (qs.get("class", [""])[0])
                typ_b = (qs.get("type", [""])[0])
                ctype = resp.headers.get("content-type", "")
                try:
                    body = resp.body()
                except Exception:
                    body = b""
                router_calls.append({
                    "class": b64d(cls_b),
                    "type": b64d(typ_b),
                    "url": resp.url[:200],
                    "ct": ctype,
                    "len": len(body),
                    "body_preview": body[:600].decode("utf-8", errors="replace"),
                })

        page.on("response", on_response)

        page.goto("https://ioly.iroquois.com/infopost/",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(6000)

        # Step 1: Find all tree nodes (x-tree-node-text spans) and double-click each
        # Step 2: Wait, then enumerate again to pick up children
        print(">> First pass: identify top-level tree nodes")
        first_pass = page.evaluate("""
            (() => {
                const nodes = [...document.querySelectorAll('.x-tree-node-text')];
                return nodes.map(n => n.innerText.trim()).filter(Boolean);
            })()
        """)
        print(f"   {len(first_pass)} nodes: {first_pass}")

        # Double-click Capacity to expand it
        for label in ("Capacity", "Locations", "Notices"):
            print(f">> Double-clicking '{label}'")
            page.evaluate(f"""
                (() => {{
                    const nodes = [...document.querySelectorAll('.x-tree-node-text')];
                    const tgt = nodes.find(n => n.innerText.trim() === {label!r});
                    if (tgt) {{
                        const event = new MouseEvent('dblclick', {{ bubbles: true, cancelable: true }});
                        tgt.dispatchEvent(event);
                    }}
                }})()
            """)
            page.wait_for_timeout(3000)

        # Second pass — children should now be visible
        print(">> Second pass: enumerate all tree nodes after expansion")
        second_pass = page.evaluate("""
            (() => {
                const nodes = [...document.querySelectorAll('.x-tree-node-text')];
                return nodes.map(n => n.innerText.trim()).filter(Boolean);
            })()
        """)
        print(f"   {len(second_pass)} nodes: {second_pass}")

        # Now click each new node that wasn't there in pass 1
        new_nodes = [n for n in second_pass if n not in first_pass]
        print(f">> New nodes after expand: {new_nodes}")
        for label in new_nodes:
            print(f">> Clicking '{label}'")
            page.evaluate(f"""
                (() => {{
                    const nodes = [...document.querySelectorAll('.x-tree-node-text')];
                    const tgt = nodes.find(n => n.innerText.trim() === {label!r});
                    if (tgt) tgt.click();
                }})()
            """)
            page.wait_for_timeout(4000)

        print()
        print(f">> Captured {len(router_calls)} RouterClass.php calls:")
        seen = set()
        for c in router_calls:
            key = (c["class"], c["type"])
            if key in seen:
                continue
            seen.add(key)
            print(f"   class={c['class']!r:<45s} type={c['type']!r:<40s} len={c['len']:>6} ct={c['ct'][:20]}")
            if c["len"] > 100 and "error" not in c["body_preview"].lower():
                print(f"      preview: {c['body_preview'][:300]!r}")

        browser.close()


if __name__ == "__main__":
    main()
