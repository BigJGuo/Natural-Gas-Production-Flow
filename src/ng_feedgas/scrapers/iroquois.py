"""Iroquois Gas Transmission System scraper.

Iroquois's IOL (InfoPost) site is a Sencha ExtJS SPA fronted by Imperva/Incapsula
bot protection. Bare HTTP requests are blocked with 403; we drive headless
Chromium via Playwright to navigate the menu tree and pull the rendered OAC grid.

Architecture (verified 2026-05-27 via tools/probe_iroquois3.py):
  Endpoint:  https://ioly.iroquois.com/infopost/classes/common/RouterClass.php
             with base64-encoded `class` and `type` query params.
  Auth class: OperationallyAvailableClass
  Cycle list: type=getCycleDescCpctyOperAvail returns the day's cycles.
  The data fetch is gated by an internal session/state that's only populated
  after the user clicks through the tree, so we drive a real browser rather
  than spelunk for the raw JSON params.

Navigation path:
  Capacity (parent tree node)
    └─ Operationally Available (leaf, xtype="app-operationallyavailable", id=101)

The OAC grid renders as an ExtJS panel with columns:
  Loc | Loc Name | Loc Purp Desc | Flow Ind | Design Capacity |
  Operating Capacity | Total Scheduled Quantity | OAC | Loc Zn | IT

Volumes are in Dth/d. Divide by 1000 for MMcf/d.

Canonical meter points for this scraper:
  Loc 90 / Waddington — Canadian-border RECEIPT (imports from TC Mainline)
  Loc 226 / Brookfield — Canadian-border DELIVERY (exports to TC Mainline)
"""
from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError, ScraperError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

ENTRY_URL = "https://ioly.iroquois.com/infopost/"

# Map our canonical cycle names to Iroquois cycleValue (verified from
# getCycleDescCpctyOperAvail response). The site uses title-cased English.
CYCLE_TO_IROQUOIS = {
    "timely": "Timely",
    "evening": "Evening",
    "intraday1": "Intraday 1",
    "intraday2": "Intraday 2",
    "intraday3": "Intraday 3",
    "confirmed": "Post",   # closest analog
}


class IroquoisScraper(BaseScraper):
    name = "iroquois"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []
        try:
            from playwright.sync_api import sync_playwright  # local import — heavy
        except ImportError as exc:
            raise ScraperError(
                "Playwright not installed. Run: pip install playwright && "
                "python -m playwright install chromium"
            ) from exc

        rows = self._fetch_rows_via_playwright(ctx, sync_playwright)
        log.info("Iroquois: parsed %d rows", len(rows))
        return _match_meters(rows, ctx.meter_points, ctx, ENTRY_URL)

    def _fetch_rows_via_playwright(self, ctx: ScrapeContext, sync_playwright) -> list[dict]:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                bctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1366, "height": 900},
                )
                page = bctx.new_page()
                log.info("Iroquois: loading entry page")
                page.goto(ENTRY_URL, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(5000)

                # Expand the Capacity tree node
                log.info("Iroquois: expanding Capacity tree")
                page.evaluate("""
                    (() => {
                        const tgt = [...document.querySelectorAll('.x-tree-node-text')]
                            .find(n => n.innerText.trim() === 'Capacity');
                        if (tgt) {
                            const ev = new MouseEvent('dblclick',
                                {bubbles: true, cancelable: true});
                            tgt.dispatchEvent(ev);
                        }
                    })()
                """)
                page.wait_for_timeout(2500)

                # Click Operationally Available leaf
                log.info("Iroquois: clicking Operationally Available")
                page.evaluate("""
                    (() => {
                        const tgt = [...document.querySelectorAll('.x-tree-node-text')]
                            .find(n => n.innerText.trim() === 'Operationally Available');
                        if (tgt) tgt.click();
                    })()
                """)
                # Wait for the grid to render
                page.wait_for_timeout(8000)

                # Some grids need an explicit "Retrieve" or "Search" button click
                # after the panel loads — try common labels
                for label in ("Retrieve", "Search", "View Report", "Submit"):
                    btn_clicked = page.evaluate(f"""
                        (() => {{
                            const btns = [...document.querySelectorAll('a, button, span')]
                                .filter(b => (b.innerText||'').trim() === {label!r});
                            if (btns.length) {{ btns[0].click(); return true; }}
                            return false;
                        }})()
                    """)
                    if btn_clicked:
                        log.info("Iroquois: clicked %s button", label)
                        page.wait_for_timeout(6000)
                        break

                # ExtJS grid uses virtual scrolling: only visible rows are in the
                # DOM at any time. Scroll top→bottom multiple times, harvesting
                # rows on each pass, to capture every row.
                log.info("Iroquois: scrolling grid to capture all virtual rows")
                seen: set[tuple] = set()
                collected: list[list[str]] = []
                for scroll_to in (0, 999999, 0, 999999, 0):
                    page.evaluate(f"""
                        (() => {{
                            const view = document.querySelector('.x-grid-view');
                            if (view) view.scrollTop = {scroll_to};
                        }})()
                    """)
                    page.wait_for_timeout(800)
                    batch = page.evaluate("""
                        (() => {
                            const rows = [...document.querySelectorAll('.x-grid-row')];
                            return rows.map(tr => [...tr.querySelectorAll('.x-grid-cell-inner')]
                                .map(c => (c.innerText || '').trim()));
                        })()
                    """)
                    for r in batch:
                        if not r or len(r) < 4:
                            continue
                        key = tuple(r[:2])
                        if key in seen:
                            continue
                        seen.add(key)
                        collected.append(r)
                log.info("Iroquois: collected %d unique grid rows", len(collected))
                return [r for r in (_normalize_grid_row(c) for c in collected) if r]
            finally:
                browser.close()


# Iroquois OAC grid column order (verified 2026-05-27 via tools/dump_iroquois.py):
#   Col 0  Loc Name           e.g. "Waddington"
#   Col 1  Loc Id             e.g. "2250"
#   Col 2  Loc Purp Desc      e.g. "Delivery point(s) quantity"
#   Col 3  Flow Ind           e.g. "Y" (active) / "N"
#   Col 4  Design Capacity
#   Col 5  Operating Capacity
#   Col 6  Total Scheduled Quantity   <-- THIS is the feedgas value
#   Col 7  OAC
#   Col 8  Loc Zn
#   Col 9  IT indicator
#
# Name-vs-ID order is REVERSED from typical FERC EBBs.
_LOC_ID_RE = re.compile(r"^\d{1,7}$")


def _normalize_grid_row(cells: list[str]) -> dict | None:
    if not cells or len(cells) < 4:
        return None
    cells = list(cells) + [""] * max(0, 10 - len(cells))
    # Accept rows where col[1] looks like a Loc ID (anchor); skip otherwise.
    if not _LOC_ID_RE.match(cells[1]):
        return None
    return {
        "loc_name": cells[0],
        "loc_id":   cells[1],
        "loc_purp": cells[2],
        "flow_ind": cells[3],
        "design":   cells[4],
        "opcap":    cells[5],
        "tsq":      cells[6],
        "oac":      cells[7],
        "loc_zn":   cells[8],
        "it":       cells[9],
    }


def _match_meters(
    rows: list[dict],
    meters: list[MeterPoint],
    ctx: ScrapeContext,
    source_url: str,
) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.utcnow()
    rows = [r for r in rows if r]
    for mp in meters:
        match = _row_match(rows, mp)
        if match is None:
            log.warning(
                "Iroquois: no row matched terminal=%s meter_id=%r location_name=%r",
                mp.terminal, mp.meter_id, mp.location_name,
            )
            continue
        tsq = _parse_number(match["tsq"])
        if tsq is None:
            log.warning("Iroquois: unparseable TSQ for %s row=%r", mp.terminal, match)
            continue
        mmcfd = tsq / 1000.0
        direction = _dir(match["flow_ind"]) or mp.direction
        out.append(FlowRecord(
            gas_day=ctx.gas_day,
            cycle=ctx.cycle,
            terminal=mp.terminal,
            pipeline=mp.pipeline,
            meter_point=match["loc_name"],
            mmcfd=mmcfd,
            direction=direction,  # type: ignore[arg-type]
            source_url=source_url,
            scraped_at=now,
        ))
        log.info(
            "Iroquois: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["loc_id"], match["loc_name"], mmcfd, match["flow_ind"],
        )
    return out


def _row_match(rows: list[dict], mp: MeterPoint) -> dict | None:
    want_dir = (mp.direction or "")[:1].upper()
    if mp.meter_id:
        hits = [r for r in rows if r["loc_id"] == str(mp.meter_id).strip()]
        if hits:
            for r in hits:
                if r["flow_ind"][:1].upper() == want_dir:
                    return r
            return hits[0]
    needle = mp.location_name.lower()
    hits = [r for r in rows if needle in r["loc_name"].lower()]
    if hits:
        for r in hits:
            if r["flow_ind"][:1].upper() == want_dir:
                return r
        return hits[0]
    return None


_NUMBER_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _parse_number(s) -> float | None:
    if s is None:
        return None
    text = str(s).replace(",", "").strip()
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _dir(f: str) -> Direction | None:
    f_low = (f or "").lower()
    if "receipt" in f_low or f_low.startswith("r"):
        return "receipt"
    if "delivery" in f_low or f_low.startswith("d"):
        return "delivery"
    return None
