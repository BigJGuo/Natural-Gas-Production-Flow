"""Williams 1Line — Transcontinental Gas Pipe Line (Transco) scraper.

The Operationally Available Capacity (OAC) report flow:

  1. GET   https://www.1line.williams.com/Transco/info-postings/capacity/Operationally-Available_NEW.html
     (sets the JSESSIONID cookie via 1Line's application gateway).
  2. GET   /ebbCode/OACQueryRequest.jsp?BUID=80&type=OAC
     (renders the form; harvests no real state — the form has no viewstate, just a few
     hidden fields like MapID=0, reportType=OAC).
  3. POST  /ebbCode/OACQueryRequest.jsp?BUID=80
     with form body: MapID=0, submitflag=true, tbGasFlowBeginDate=MM/DD/YYYY,
     tbGasFlowEndDate=MM/DD/YYYY, cycle=N (1=Timely, 2=Evening, 3=ID1, 4=ID2, 8=ID3,
     5=Post, 7=Retro), locationIDs="" (or comma-separated), reportType=OAC.
     The server validates and stores the query in the session.
  4. GET   /ebbCode/OACreport.jsp
     returns the populated HTML report (~400 KB). Data rows are <TR><TD>...</TD></TR>
     blocks where the first cell is a 7-digit Loc ID. Columns:
       [0] Loc id
       [1] Loc Purp Desc  (Receipt Location | Delivery Location | Pipeline Segment ... | Storage Area)
       [2] Flow Ind       (R | D | BD | SI | SW)
       [3] QTI            (SGQ | MLQ | STQ | DPQ | ...)
       [4] Loc Name
       [5] Loc Zn
       [6] Design Capacity     (Dth/d)
       [7] Operating Capacity  (Dth/d)
       [8] Total Scheduled Quantity  (Dth/d)   <-- THIS is the feedgas value
       [9] OAC                 (Dth/d)
       [10] IT Indicator
       [11] All Qty Avail
       [12] Qty Reason

Important coverage caveat: Transco's public OAC report exposes mainline segments,
storage areas, and 223 named "Delivery Location" points — but LNG terminal points
are NOT clearly labeled. After Phase-0 investigation:
- **Freeport LNG** is *almost certainly* Loc 9009310 "LIGHTHOUSE ROAD M4662 MP 13"
  (MP 13 = Quintana Island area, scheduled qty ~1,060 MMcf/d matches Freeport
  feedgas). VERIFY against EIA before relying on it.
- **Cove Point** could not be unambiguously identified. Default config leaves
  the meter_id null until a human can confirm via browser.

Volumes are quoted in Dth/d. Divide by 1000 to get MMcf/d (industry convention).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError, ScraperError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

LANDING_URL = "https://www.1line.williams.com/Transco/info-postings/capacity/Operationally-Available_NEW.html"
FORM_URL = "https://www.1line.williams.com/ebbCode/OACQueryRequest.jsp?BUID=80&type=OAC"
POST_URL = "https://www.1line.williams.com/ebbCode/OACQueryRequest.jsp?BUID=80"
REPORT_URL = "https://www.1line.williams.com/ebbCode/OACreport.jsp"

CYCLE_TO_WILLIAMS = {
    "timely": "1",
    "evening": "2",
    "intraday1": "3",
    "intraday2": "4",
    "intraday3": "8",
    "confirmed": "5",   # "Post" cycle = final scheduled quantity for the gas day
}


class WilliamsScraper(BaseScraper):
    name = "williams"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        cycle_code = CYCLE_TO_WILLIAMS.get(ctx.cycle)
        if not cycle_code:
            raise ScraperError(f"Williams: unsupported cycle {ctx.cycle!r}")

        # Establish session
        log.info("Williams: bootstrap session")
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        r0 = self.session.get(LANDING_URL, timeout=30)
        r0.raise_for_status()

        self.session.headers["Referer"] = LANDING_URL
        r1 = self.session.get(FORM_URL, timeout=30)
        r1.raise_for_status()

        # Submit the form. If specific meter_ids are configured, request only those
        # (much smaller payload). Otherwise, pull all locations.
        configured_ids = [mp.meter_id for mp in ctx.meter_points if mp.meter_id]
        location_ids = ",".join(configured_ids) if configured_ids else ""

        flow_day = ctx.gas_day.strftime("%m/%d/%Y")
        post_data = {
            "MapID": "0",
            "submitflag": "true",
            "tbGasFlowBeginDate": flow_day,
            "tbGasFlowEndDate": flow_day,
            "cycle": cycle_code,
            "locationIDs": location_ids,
            "reportType": "OAC",
        }
        log.info("Williams POST OAC query: day=%s cycle=%s loc_ids=%r",
                 flow_day, cycle_code, location_ids or "ALL")
        self.session.headers["Referer"] = POST_URL
        r2 = self.session.post(POST_URL, data=post_data, timeout=60)
        r2.raise_for_status()

        # Fetch the actual report (session now contains the query parameters)
        log.info("Williams GET OAC report")
        self.session.headers["Referer"] = POST_URL
        self._sleep(ctx)
        r3 = self.session.get(REPORT_URL, timeout=120)
        r3.raise_for_status()

        rows = parse_oac_report(r3.text)
        log.info("Williams: parsed %d data rows", len(rows))
        return _match_meters(rows, ctx.meter_points, ctx, r3.url)


# ---------- HTML parsing ----------

_TR_RE = re.compile(r"<TR[^>]*>(.*?)</TR>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<TD[^>]*>(.*?)</TD>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_LOC_ID_RE = re.compile(r"^\d{7}$")


def parse_oac_report(html: str) -> list[dict]:
    """Return list of dicts with canonical keys."""
    rows: list[dict] = []
    for tr_html in _TR_RE.findall(html):
        cells_raw = _TD_RE.findall(tr_html)
        if not cells_raw:
            continue
        cells = [_TAG_RE.sub("", c).strip() for c in cells_raw]
        if not _LOC_ID_RE.match(cells[0]):
            continue
        if len(cells) < 10:
            continue
        rows.append({
            "loc_id":      cells[0],
            "loc_purp":    cells[1] if len(cells) > 1 else "",
            "flow_ind":    cells[2] if len(cells) > 2 else "",
            "qti":         cells[3] if len(cells) > 3 else "",
            "loc_name":    cells[4] if len(cells) > 4 else "",
            "loc_zn":      cells[5] if len(cells) > 5 else "",
            "design":      cells[6] if len(cells) > 6 else "",
            "opcap":       cells[7] if len(cells) > 7 else "",
            "tsq":         cells[8] if len(cells) > 8 else "",
            "oac":         cells[9] if len(cells) > 9 else "",
        })
    if not rows:
        raise ParseError("Williams: no data rows parsed from OAC report")
    return rows


def _match_meters(
    rows: list[dict],
    meters: Iterable[MeterPoint],
    ctx: ScrapeContext,
    source_url: str,
) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.utcnow()
    for mp in meters:
        match = _row_match(rows, mp)
        if not match:
            log.warning(
                "Williams: no row matched terminal=%s meter_id=%r location_name=%r",
                mp.terminal, mp.meter_id, mp.location_name,
            )
            continue
        tsq = _parse_number(match["tsq"])
        if tsq is None:
            log.warning("Williams: unparseable TSQ for %s row=%r", mp.terminal, match)
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
            "Williams: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["loc_id"], match["loc_name"], mmcfd, match["flow_ind"],
        )
    return out


def _row_match(rows: list[dict], mp: MeterPoint) -> dict | None:
    want_dir = (mp.direction or "").strip()[:1].upper()  # 'D' or 'R'
    if mp.meter_id:
        hits = [r for r in rows if r["loc_id"] == str(mp.meter_id).strip()]
        if hits:
            # Prefer the row whose flow_ind matches the expected direction.
            for r in hits:
                if r["flow_ind"].strip().upper() == want_dir:
                    return r
            # 'BD' (bidirectional) is acceptable as either direction
            for r in hits:
                if r["flow_ind"].strip().upper() == "BD":
                    return r
            return hits[0]
    needle = mp.location_name.lower()
    hits = [r for r in rows if needle in r["loc_name"].lower()]
    if hits:
        for r in hits:
            if r["flow_ind"].strip().upper() == want_dir:
                return r
        return hits[0]
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
    f = (flow or "").strip().upper()
    if f == "D":
        return "delivery"
    if f == "R":
        return "receipt"
    if f == "BD":
        return "delivery"
    return None
