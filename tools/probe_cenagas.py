"""Probe CENAGAS (Mexico's gas system operator) for daily import-by-point data.

CENAGAS publishes free public dashboards at boletin-gestor.cenagas.gob.mx.
Many of the relevant border-injection points are intrastate-fed from Texas
(Trans-Pecos, Comanche Trail, NET Mexico, Valley Crossing) and so don't show
up on US-side FERC EBBs. CENAGAS's Spanish-side data is the authoritative
free daily source for these flows.

Goal: identify the AJAX endpoints that return per-point daily flow data.

Run:  python tools/probe_cenagas.py
"""
from __future__ import annotations

import re
from playwright.sync_api import sync_playwright


PAGES_TO_PROBE = [
    "https://boletin-gestor.cenagas.gob.mx/GestionTecnica/Volumen",
    "https://boletin-gestor.cenagas.gob.mx/Multimedia/InyeccionesExtracciones",
    "https://boletin-gestor.cenagas.gob.mx/Multimedia/ParametrosOperativos",
]


def main() -> None:
    api_calls: list[dict] = []

    with sync_playwright() as p:
        # Ignore certificate errors — CENAGAS uses chain that may not validate
        browser = p.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
        ctx = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()

        def on_response(resp):
            url = resp.url
            ct = resp.headers.get("content-type", "")
            # Capture anything that looks like API/JSON or data fetches
            if "boletin-gestor.cenagas.gob.mx" in url and (
                "json" in ct.lower()
                or "/api/" in url
                or "/Volumen" in url
                or "/Inyecciones" in url
                or "/Extracciones" in url
                or "/Parametros" in url
                or "?from" in url
            ):
                try:
                    body = resp.body()[:600]
                except Exception:
                    body = b""
                api_calls.append({
                    "url": url,
                    "ct": ct,
                    "len": len(body),
                    "preview": body.decode("utf-8", errors="replace"),
                })

        page.on("response", on_response)

        for url in PAGES_TO_PROBE:
            print(f"\n>> visit: {url}")
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception as exc:
                print(f"   error: {exc}")
                continue
            page.wait_for_timeout(8000)
            print(f"   title: {page.title()!r}")

        print()
        print(f">> Captured {len(api_calls)} API calls:")
        seen = set()
        for c in api_calls:
            key = c["url"].split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            print(f"   {c['ct'][:30]:<30}  {c['url']}")
            if c["preview"].strip() and "html" not in c["ct"]:
                p2 = c["preview"][:200].replace("\n", " ")
                print(f"        preview: {p2!r}")

        browser.close()


if __name__ == "__main__":
    main()
