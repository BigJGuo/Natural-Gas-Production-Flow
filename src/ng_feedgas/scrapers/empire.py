"""Empire Pipeline (National Fuel) — PeopleSoft OAC scraper (Playwright).

Empire's Operationally Available Capacity is a PUBLIC (no-login) PeopleSoft
component:
  https://sbsprd2.natfuel.com/psc/sbsprd/NFSBS/SBSPRD/c/NFOM_INFORMATIONAL_POSTINGS.NFOC_OPER_AVAIL_1.GBL

The page exposes three CSV-download links for the current gas day's postings,
identified by element id NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0/$1/$2:
  $0 = Evening, $1 = Timely, $2 = Prelim (the three most-recent postings).
Clicking one fires a real browser download of a full OAC CSV. We drive it with
Playwright (the download is a stateful PeopleSoft ICAction, impractical over
plain requests) and capture the file with expect_download().

CSV layout: ~20 metadata lines, then a header row with columns including
  Loc Name, Loc, Loc Purp Desc, ..., Total Scheduled Quantity, Flow Indicator
TSQ is in Dth/d; /1000 = MMcf/d.

SNAPSHOT-ONLY: the page loads the current gas day; --date for past days is not
supported (like et_ipost / iroquois). Cycle selects the download button.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

import pandas as pd

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

OAC_URL = ("https://sbsprd2.natfuel.com/psc/sbsprd/NFSBS/SBSPRD/c/"
           "NFOM_INFORMATIONAL_POSTINGS.NFOC_OPER_AVAIL_1.GBL")

# Our canonical cycle -> CSV-download button index ($0=Evening, $1=Timely, $2=Prelim).
CYCLE_TO_BTN = {
    "evening": "0",
    "confirmed": "0",
    "intraday1": "0",   # Empire posts Evening/Timely/Prelim; use most-recent for intraday
    "intraday2": "0",
    "intraday3": "0",
    "timely": "1",
}


class EmpireScraper(BaseScraper):
    name = "empire"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []
        btn = CYCLE_TO_BTN.get(ctx.cycle, "0")
        content = self._download_csv(btn)
        df = _parse_csv(content)
        return _match_meters(df, ctx, OAC_URL)

    def _download_csv(self, btn_idx: str) -> str:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(accept_downloads=True)
                page.goto(OAC_URL, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(2500)
                sel = f"[id='NF_FILE_ATT_WRK_NF_CSV_DWN_BTN${btn_idx}']"
                with page.expect_download(timeout=20000) as dl_info:
                    page.locator(sel).click(timeout=8000)
                dl = dl_info.value
                path = dl.path()
                if not path:
                    raise ParseError("Empire: download produced no file")
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read()
            finally:
                browser.close()


def _parse_csv(content: str) -> pd.DataFrame:
    lines = content.splitlines()
    hdr_i = next((i for i, l in enumerate(lines)
                  if re.search(r"\bloc\b", l, re.I) and l.count(",") >= 3), None)
    if hdr_i is None:
        raise ParseError("Empire: could not locate CSV header row")
    df = pd.read_csv(io.StringIO(content), skiprows=hdr_i)
    df.columns = [c.strip() for c in df.columns]
    required = ["Loc Name", "Loc", "Total Scheduled Quantity", "Flow Indicator"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ParseError(f"Empire: missing columns {missing}; got {list(df.columns)}")
    df["Loc"] = df["Loc"].astype(str).str.strip()
    log.info("Empire: parsed %d rows", len(df))
    return df


def _match_meters(df: pd.DataFrame, ctx: ScrapeContext, source_url: str) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    for mp in ctx.meter_points:
        match = _row_match(df, mp)
        if match is None:
            log.warning("Empire: no row matched terminal=%s meter_id=%r name=%r",
                        mp.terminal, mp.meter_id, mp.location_name)
            continue
        tsq = _parse_number(match["Total Scheduled Quantity"])
        if tsq is None:
            continue
        out.append(FlowRecord(
            gas_day=ctx.gas_day, cycle=ctx.cycle, terminal=mp.terminal,
            pipeline=mp.pipeline, meter_point=str(match["Loc Name"]).strip(),
            mmcfd=tsq / 1000.0,
            direction=_dir(str(match.get("Flow Indicator", ""))) or mp.direction,  # type: ignore[arg-type]
            source_url=source_url, scraped_at=now,
        ))
        log.info("Empire: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
                 mp.terminal, match["Loc"], match["Loc Name"], tsq / 1000.0,
                 match["Flow Indicator"])
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint):
    if mp.meter_id:
        hits = df[df["Loc"] == str(mp.meter_id).strip()]
        if not hits.empty:
            for _, r in hits.iterrows():
                if _dir(str(r.get("Flow Indicator", ""))) == mp.direction:
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
    m = re.search(r"-?[\d.]+", text)
    return float(m.group(0)) if m else None


def _dir(f: str) -> Direction | None:
    f_low = (f or "").strip().lower()
    if "delivery" in f_low or f_low == "d":
        return "delivery"
    if "receipt" in f_low or f_low == "r":
        return "receipt"
    return None
