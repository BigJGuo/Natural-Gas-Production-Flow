"""TC Energy "Ganesha" EBB (tcplus.com) scraper.

Covers the TC Energy interstate pipelines hosted on tcplus.com:
  GTN          — Gas Transmission Northwest  (Kingsgate, ID — Canada import ~2 Bcf/d)
  Great Lakes  — Great Lakes Gas Transmission (Emerson, MN import; St. Clair crossing)
  North Baja   — North Baja Pipeline          (Ogilby, CA — Mexico export)
  Tuscarora    — Tuscarora Gas Transmission

Each pipeline exposes an Operationally Available Capacity report. The page's CSV
button POSTs to:
  https://www.tcplus.com/{PIPELINE}/Export/Generate
with form fields (reverse-engineered from printexportconfiguration.js):
  serviceTypeName = Ganesha.InfoPost.Service.OperationalCapacity.OperationalCapacityService, Ganesha.InfoPost.Service
  filterTypeName  = Ganesha.InfoPost.ViewModels.GasDayAndCycleTypeFilterViewModel, Ganesha.InfoPost
  templateType    = 6        (the page's hidden #ExportEnum value)
  exportType      = 1        (1=CSV, 2=Excel, 3=PDF, 4=Txt, 5=Tab)
  filter.GasDay   = MM/DD/YY
  filter.CycleType= 1 Timely | 4 Evening | 2 Intraday 1 | 3 Intraday 2 | 5 Intraday 3
  customExtension = (empty)

The CSV has 3 metadata rows + 1 blank row, then a header row (index 4) with:
  Loc Name, Loc, Loc Purp Desc, Loc/QTI, Flow Ind, DC, OPC, TSQ, OAC, IT, All Qty Avail
TSQ is in MMBtu/d (Meas Basis = Million BTU's); divide by 1000 for MMcf/d.

Honors --date and --cycle. The pipeline URL path is resolved from
MeterPoint.pipeline_code via PIPE_PATHS so the YAML can use a clean code.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import pandas as pd

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

BASE = "https://www.tcplus.com"
OAC_PAGE_TMPL = BASE + "/{path}/OperationalCapacity"
EXPORT_TMPL = BASE + "/{path}/Export/Generate"

SERVICE_TYPE = ("Ganesha.InfoPost.Service.OperationalCapacity."
                "OperationalCapacityService, Ganesha.InfoPost.Service")
FILTER_TYPE = ("Ganesha.InfoPost.ViewModels.GasDayAndCycleTypeFilterViewModel, "
               "Ganesha.InfoPost")
TEMPLATE_TYPE = "6"
EXPORT_CSV = "1"

# MeterPoint.pipeline_code -> tcplus URL path segment (some contain spaces).
PIPE_PATHS = {
    "GTN": "GTN",
    "GLGT": "Great Lakes",
    "NBAJA": "North Baja",
    "TUS": "Tuscarora",
}

# Our canonical cycle -> tcplus CycleType value.
CYCLE_TYPE = {
    "timely": "1",
    "evening": "4",
    "intraday1": "2",
    "intraday2": "3",
    "intraday3": "5",
    "confirmed": "4",   # no separate "confirmed" cycle on this EBB; Evening is final
}

HEADER_ROW = 4  # 0-based index of the column-header row in the exported CSV


class TCPlusScraper(BaseScraper):
    name = "tcplus"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        return self.with_retry(lambda: self._fetch_impl(ctx), label="tcplus")

    def _fetch_impl(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        cycle_type = CYCLE_TYPE.get(ctx.cycle)
        if not cycle_type:
            raise ParseError(f"tcplus: unsupported cycle {ctx.cycle!r}")

        by_code: dict[str, list[MeterPoint]] = {}
        for mp in ctx.meter_points:
            code = mp.pipeline_code.upper()
            if code not in PIPE_PATHS:
                log.warning("tcplus: unknown pipeline_code %r for terminal=%s "
                            "(known: %s)", code, mp.terminal, sorted(PIPE_PATHS))
                continue
            by_code.setdefault(code, []).append(mp)

        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )

        records: list[FlowRecord] = []
        for code, meters in by_code.items():
            path = PIPE_PATHS[code]
            df, url = self._download(ctx, path, cycle_type)
            records.extend(_match_meters(df, meters, ctx, url))
            self._sleep(ctx)
        return records

    def _download(self, ctx: ScrapeContext, path: str, cycle_type: str):
        # Visit the OAC page first so the session carries the expected cookies.
        self.session.get(OAC_PAGE_TMPL.format(path=path), timeout=30)
        data = {
            "serviceTypeName": SERVICE_TYPE,
            "filterTypeName": FILTER_TYPE,
            "templateType": TEMPLATE_TYPE,
            "exportType": EXPORT_CSV,
            "filter.GasDay": ctx.gas_day.strftime("%m/%d/%y"),
            "filter.CycleType": cycle_type,
            "customExtension": "",
        }
        url = EXPORT_TMPL.format(path=path)
        log.info("tcplus %s: POST Export/Generate day=%s cycle=%s",
                 path, data["filter.GasDay"], cycle_type)
        r = self.session.post(
            url, data=data, timeout=120,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "csv" not in ct and "octet" not in ct and "text/plain" not in ct:
            raise ParseError(
                f"tcplus {path}: unexpected Content-Type {ct!r}; "
                f"first 200 bytes: {r.content[:200]!r}"
            )
        try:
            df = pd.read_csv(io.BytesIO(r.content), skiprows=HEADER_ROW)
        except Exception as exc:
            raise ParseError(f"tcplus {path}: failed to parse CSV: {exc}") from exc
        df.columns = [c.strip() for c in df.columns]
        required = ["Loc Name", "Loc", "Flow Ind", "TSQ"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ParseError(
                f"tcplus {path}: missing columns {missing}; got {list(df.columns)}"
            )
        df["Loc"] = df["Loc"].astype(str).str.strip()
        log.info("tcplus %s: parsed %d rows", path, len(df))
        return df, r.url


def _match_meters(
    df: pd.DataFrame,
    meters: list[MeterPoint],
    ctx: ScrapeContext,
    source_url: str,
) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    for mp in meters:
        match = _row_match(df, mp)
        if match is None:
            log.warning(
                "tcplus: no row matched terminal=%s pipeline_code=%s meter_id=%r "
                "location_name=%r", mp.terminal, mp.pipeline_code, mp.meter_id,
                mp.location_name,
            )
            continue
        tsq = _parse_number(match["TSQ"])
        if tsq is None:
            continue
        mmcfd = tsq / 1000.0
        direction = _dir(str(match.get("Flow Ind", ""))) or mp.direction
        out.append(FlowRecord(
            gas_day=ctx.gas_day,
            cycle=ctx.cycle,
            terminal=mp.terminal,
            pipeline=mp.pipeline,
            meter_point=str(match["Loc Name"]).strip(),
            mmcfd=mmcfd,
            direction=direction,  # type: ignore[arg-type]
            source_url=source_url,
            scraped_at=now,
        ))
        log.info("tcplus: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
                 mp.terminal, match["Loc"], match["Loc Name"], mmcfd, match["Flow Ind"])
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint):
    if mp.meter_id:
        hits = df[df["Loc"] == str(mp.meter_id).strip()]
        if not hits.empty:
            for _, r in hits.iterrows():
                if _dir(str(r.get("Flow Ind", ""))) == mp.direction:
                    return r
            return hits.iloc[0]
    needle = mp.location_name.lower()
    hits = df[df["Loc Name"].astype(str).str.lower().str.contains(needle, regex=False, na=False)]
    if not hits.empty:
        return hits.iloc[0]
    return None


def _parse_number(s) -> float | None:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    text = str(s).replace(",", "").strip()
    if not text or text in ("-", "NA", "nan"):
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
    # BD (bidirectional) is ambiguous; let the caller fall back to configured dir.
    return None
