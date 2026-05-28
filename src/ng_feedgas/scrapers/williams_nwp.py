"""Williams Northwest Pipeline (NWP) scraper.

Despite both being Williams properties, Transco and Northwest Pipeline use
COMPLETELY DIFFERENT portals. Transco lives on 1line.williams.com (Java
Servlet + JSP, BUID-keyed). Northwest Pipeline has its own Apache Struts
portal at `www.northwest.williams.com/NWP_Portal/`.

Endpoint:
  https://www.northwest.williams.com/NWP_Portal/CapacityResultsScrollable.action
    ?StartGasFlowDate=MM-DD-YYYY
    &EndGasFlowDate=MM-DD-YYYY
    &RptType=OA      (Operationally Available)
    &RptPart=ALL

The page returns HTML with a single Kendo-grid table whose rows have:
  Col 0  Loc id        (e.g. 297)
  Col 1  Loc Name      (e.g. SUMAS RECEIPT)
  Col 2  Loc Zone      (28219)
  Col 3  Loc Purp Desc (Receipt Location | Delivery Location | Mainline ...)
  Col 4  Flow Ind      (Receipt | Delivery | Mainline | ...)
  Col 5  QTI           (RPQ | DPQ | MLQ | ...)
  Col 6  Design Cap    (Dth/d)
  Col 7  Operating Cap (Dth/d)
  Col 8  Total Sched   (Dth/d)   <-- THIS is the value we want
  Col 9  OAC           (Dth/d)
  Col 10 IT indicator  (often blank)

The page also exposes a CSV export endpoint but it's Kendo-grid-client-side
(JavaScript builds the CSV from the in-memory grid model), so we parse the
HTML rows directly.

There is NO cycle dropdown — the page always shows the most-recent posted
snapshot. We log the cycle description the page reports (e.g. "Intra Day 1
Cycle 3") and store the requested cycle name as supplied.

Sumas Receipt is the canonical Canada→US import point (Loc 297). Kingsgate
Receipt is the secondary Canada import (eastern WA/ID border).

Volumes are in Dth/d. Divide by 1000 for MMcf/d.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterable

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError, ScraperError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

REPORT_URL = "https://www.northwest.williams.com/NWP_Portal/CapacityResultsScrollable.action"


class WilliamsNWPScraper(BaseScraper):
    name = "williams_nwp"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        return self.with_retry(lambda: self._fetch_impl(ctx))

    def _fetch_impl(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        gas_day = ctx.gas_day.strftime("%m-%d-%Y")
        params = {
            "StartGasFlowDate": gas_day,
            "EndGasFlowDate": gas_day,
            "RptType": "OA",
            "RptPart": "ALL",
        }
        log.info("Williams NWP: GET capacity report for %s", gas_day)
        r = self.session.get(REPORT_URL, params=params, timeout=120)
        r.raise_for_status()

        rows = parse_nwp_html(r.text)
        log.info("Williams NWP: parsed %d data rows", len(rows))
        return _match_meters(rows, ctx.meter_points, ctx, r.url)


# ---------- HTML parsing ----------

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_LOC_ID_RE = re.compile(r"^\d{2,6}$")


def parse_nwp_html(html: str) -> list[dict]:
    """Return list of dicts with canonical keys."""
    rows: list[dict] = []
    for tr_html in _TR_RE.findall(html):
        tr_clean = _COMMENT_RE.sub("", tr_html)
        cells_raw = _TD_RE.findall(tr_clean)
        if len(cells_raw) < 9:
            continue
        cells = [_TAG_RE.sub("", c).strip() for c in cells_raw]
        if not _LOC_ID_RE.match(cells[0]):
            continue
        rows.append({
            "loc_id":   cells[0],
            "loc_name": cells[1] if len(cells) > 1 else "",
            "loc_zn":   cells[2] if len(cells) > 2 else "",
            "loc_purp": cells[3] if len(cells) > 3 else "",
            "flow_ind": cells[4] if len(cells) > 4 else "",
            "qti":      cells[5] if len(cells) > 5 else "",
            "design":   cells[6] if len(cells) > 6 else "",
            "opcap":    cells[7] if len(cells) > 7 else "",
            "tsq":      cells[8] if len(cells) > 8 else "",
            "oac":      cells[9] if len(cells) > 9 else "",
        })
    if not rows:
        raise ParseError("Williams NWP: no data rows parsed from capacity report")
    return rows


def _match_meters(
    rows: list[dict],
    meters: Iterable[MeterPoint],
    ctx: ScrapeContext,
    source_url: str,
) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    for mp in meters:
        match = _row_match(rows, mp)
        if not match:
            log.warning(
                "Williams NWP: no row matched terminal=%s meter_id=%r location_name=%r",
                mp.terminal, mp.meter_id, mp.location_name,
            )
            continue
        tsq = _parse_number(match["tsq"])
        if tsq is None:
            log.warning("Williams NWP: unparseable TSQ for %s row=%r", mp.terminal, match)
            continue
        mmcfd = tsq / 1000.0
        direction = _direction(match["flow_ind"]) or mp.direction
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
            "Williams NWP: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["loc_id"], match["loc_name"], mmcfd, match["flow_ind"],
        )
    return out


def _row_match(rows: list[dict], mp: MeterPoint) -> dict | None:
    if mp.meter_id:
        for r in rows:
            if r["loc_id"] == str(mp.meter_id).strip():
                # Prefer row matching expected direction
                if mp.direction.lower() in r["flow_ind"].lower():
                    return r
        # Fallback: first matching loc_id regardless of direction
        for r in rows:
            if r["loc_id"] == str(mp.meter_id).strip():
                return r
    needle = mp.location_name.lower()
    for r in rows:
        if needle in r["loc_name"].lower():
            # Prefer matching direction
            if mp.direction.lower() in r["flow_ind"].lower():
                return r
    for r in rows:
        if needle in r["loc_name"].lower():
            return r
    return None


_NUMBER_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _parse_number(s: str) -> float | None:
    if not s:
        return None
    text = str(s).replace(",", "").strip()
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _direction(flow: str) -> Direction | None:
    f_low = (flow or "").strip().lower()
    if "receipt" in f_low:
        return "receipt"
    if "delivery" in f_low:
        return "delivery"
    if "mainline" in f_low:
        return "delivery"  # transit points treated as delivery
    return None
