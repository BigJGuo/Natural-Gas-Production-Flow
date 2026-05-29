"""Trellis PTM (DT Midstream) EBB scraper — Viking Gas Transmission.

DT Midstream hosts its FERC infopost on Trellis Energy's PTMS at
dtmidstream.trellisenergy.com. The Operationally Available Capacity report is
public (no login) and served by two JSON endpoints (reverse-engineered 2026-05-29):

  1. List postings (jqGrid):
     /ptms/public/infopost/getInfoPostRpts.do
       ?tspId={TSP}&rptId=2&downloadInd=0&searchInd=0&showLatestInd=0
       &cycleId=&startDate=&endDate=&_search=false&nd=1&rows=200&page=1&sidx=&sord=asc&_=1
     (needs Referer = the viewInfoPostingReportTable.do page). Returns JSON
     {"rows":[{"id":..,"formattedGasDay":"MM/DD/YYYY","cycleCode":"Evening",...}]}.
     `id` is the infoPostDataId used by the data endpoint.

  2. Data file for one posting:
     /ptms/public/infopost/getInfoPostRptTxtFile.do?infoPostDataId={id}&level=1
     Returns JSON with columnNames + an `xmlData` string of <row><cell>… rows.
     Columns: loc_hidden, Loc Name, Loc, Loc Prop, Loc Purp Desc, Flow Ind,
     Loc/QTI, All Qty Avail, DC, OPC, TSQ, OAC, IT, Qty Reason.
     TSQ is MMBtu/d; /1000 = MMcf/d.

Honors --date and --cycle (the listing carries gas day + cycle). TSP code map in
TSP_IDS; currently Viking (VGT, tspId=9). rptId=2 = Operationally Available Capacity.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

BASE = "https://dtmidstream.trellisenergy.com"
HOME_TMPL = BASE + "/ptms/home/infopost/{code}"
LIST_URL = BASE + "/ptms/public/infopost/getInfoPostRpts.do"
DATA_URL = BASE + "/ptms/public/infopost/getInfoPostRptTxtFile.do"
REFERER_TMPL = BASE + "/ptms/public/infopost/viewInfoPostingReportTable.do?reportType=2&globalTSP={tsp}"

OAC_RPT_ID = "2"

# MeterPoint.pipeline_code -> Trellis tspId / infopost path code.
TSP_IDS = {"VGT": "9"}

CYCLE_TO_TRELLIS = {
    "timely": "Timely",
    "evening": "Evening",
    "intraday1": "Intraday 1",
    "intraday2": "Intraday 2",
    "intraday3": "Intraday 3",
    "confirmed": "Evening",   # no separate "confirmed" cycle here
}


class TrellisScraper(BaseScraper):
    name = "trellis"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        return self.with_retry(lambda: self._fetch_impl(ctx), label="trellis")

    def _fetch_impl(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []
        cycle_code = CYCLE_TO_TRELLIS.get(ctx.cycle)
        if not cycle_code:
            raise ParseError(f"trellis: unsupported cycle {ctx.cycle!r}")

        by_code: dict[str, list[MeterPoint]] = {}
        for mp in ctx.meter_points:
            code = mp.pipeline_code.upper()
            if code not in TSP_IDS:
                log.warning("trellis: unknown pipeline_code %r for terminal=%s "
                            "(known: %s)", code, mp.terminal, sorted(TSP_IDS))
                continue
            by_code.setdefault(code, []).append(mp)

        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        self.session.headers["X-Requested-With"] = "XMLHttpRequest"

        records: list[FlowRecord] = []
        for code, meters in by_code.items():
            tsp = TSP_IDS[code]
            # Establish session cookies the public report grid expects.
            self.session.get(HOME_TMPL.format(code=code), timeout=30)
            post_id = self._find_posting_id(ctx, tsp, cycle_code)
            if post_id is None:
                log.warning("trellis %s: no OAC posting for %s %s", code,
                            ctx.gas_day, cycle_code)
                continue
            cols, rows = self._fetch_data(post_id, tsp)
            records.extend(_match_meters(cols, rows, meters, ctx, DATA_URL))
            self._sleep(ctx)
        return records

    def _find_posting_id(self, ctx: ScrapeContext, tsp: str, cycle_code: str):
        params = {
            "tspId": tsp, "rptId": OAC_RPT_ID, "downloadInd": "0", "searchInd": "0",
            "showLatestInd": "0", "cycleId": "", "startDate": "", "endDate": "",
            "_search": "false", "nd": "1", "rows": "200", "page": "1",
            "sidx": "", "sord": "asc", "_": "1",
        }
        headers = {"Referer": REFERER_TMPL.format(tsp=tsp)}
        r = self.session.get(LIST_URL, params=params, headers=headers, timeout=60)
        r.raise_for_status()
        try:
            data = json.loads(r.text)
        except Exception as exc:
            raise ParseError(f"trellis: listing not JSON: {exc}") from exc
        want_day = ctx.gas_day.strftime("%m/%d/%Y")
        best = None
        for row in data.get("rows", []):
            if row.get("formattedGasDay") == want_day and row.get("cycleCode") == cycle_code:
                # Prefer the most-recently-run posting for that day+cycle.
                if best is None or (row.get("runDate") or "") > (best.get("runDate") or ""):
                    best = row
        return best.get("id") if best else None

    def _fetch_data(self, post_id, tsp: str):
        r = self.session.get(DATA_URL, params={"infoPostDataId": str(post_id), "level": "1"},
                             headers={"Referer": REFERER_TMPL.format(tsp=tsp)}, timeout=60)
        r.raise_for_status()
        try:
            data = json.loads(r.text)
        except Exception as exc:
            raise ParseError(f"trellis: data file not JSON: {exc}") from exc
        cols = [c.get("content") for c in data.get("columnNames", [])]
        xml = data.get("xmlData") or ""
        rows = [re.findall(r"<cell>(.*?)</cell>", rx, re.S)
                for rx in re.findall(r"<row>(.*?)</row>", xml, re.S)]
        if not cols:
            raise ParseError("trellis: no columnNames in data file")
        return cols, rows


def _match_meters(cols, rows, meters, ctx, source_url) -> list[FlowRecord]:
    try:
        i_name = cols.index("Loc Name")
        i_loc = cols.index("Loc")
        i_flow = cols.index("Flow Ind")
        i_tsq = cols.index("TSQ")
    except ValueError as exc:
        raise ParseError(f"trellis: expected column missing: {exc}; got {cols}")

    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    index: dict[str, list] = {}
    for c in rows:
        if len(c) > max(i_loc, i_tsq):
            index.setdefault(str(c[i_loc]).strip(), []).append(c)

    for mp in meters:
        match = None
        cands = index.get(str(mp.meter_id).strip(), []) if mp.meter_id else []
        for c in cands:
            if _dir(c[i_flow]) == mp.direction:
                match = c
                break
        if match is None and cands:
            match = cands[0]
        if match is None:
            # location-name contains fallback
            needle = mp.location_name.lower()
            for c in rows:
                if len(c) > i_name and needle in str(c[i_name]).lower():
                    match = c
                    break
        if match is None:
            log.warning("trellis: no row matched terminal=%s meter_id=%r name=%r",
                        mp.terminal, mp.meter_id, mp.location_name)
            continue
        tsq = _parse_number(match[i_tsq])
        if tsq is None:
            continue
        out.append(FlowRecord(
            gas_day=ctx.gas_day, cycle=ctx.cycle, terminal=mp.terminal,
            pipeline=mp.pipeline, meter_point=str(match[i_name]).strip(),
            mmcfd=tsq / 1000.0,
            direction=_dir(match[i_flow]) or mp.direction,  # type: ignore[arg-type]
            source_url=source_url, scraped_at=now,
        ))
        log.info("trellis: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
                 mp.terminal, match[i_loc], match[i_name], tsq / 1000.0, match[i_flow])
    return out


def _parse_number(s) -> float | None:
    text = str(s).replace(",", "").strip()
    if not text or text in ("-", "NA", "nan", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _dir(f: str) -> Direction | None:
    f_up = (f or "").strip().upper()
    if f_up == "D":
        return "delivery"
    if f_up == "R":
        return "receipt"
    return None
