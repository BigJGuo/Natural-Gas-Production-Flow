"""Kinder Morgan Pipeline Portal scraper.

Targets:  https://pipeline2.kindermorgan.com/Capacity/OpAvailPoint.aspx?code={CODE}

The OpAvailPoint page is an ASP.NET WebForms screen with an Infragistics WebDataGrid.
The HTML grid is paginated (75 rows per page) and rendered with viewstate-driven
postbacks — painful to scrape directly. Instead we use the page's *Excel download*
button (`btnDownload`), which returns a single .xlsx containing all rows for the
selected gas day + cycle.

Flow:
  1. GET the form page (harvests __VIEWSTATE, __EVENTVALIDATION, btnDownload name).
  2. POST with btnDownload + ctl00$hdnIsDownload=true.
  3. Read the .xlsx with pandas. Real header is at row index 3.

The KMI portal hosts many pipelines under different `code` values, including
some operated by third parties:

  Code        TSP          Pipeline                     LNG terminals served
  ----        ---          --------                     --------------------
  NGPL        6931794      Natural Gas Pipeline Co.     Sabine Pass, Corpus Christi
  EEC         828834445    Elba Express Co.             Elba Island
  TGP         1939164      Tennessee Gas Pipeline       Plaquemines, Cameron (via CIP)

So this single scraper covers 5 of 8 terminals in the POC by hitting three codes.

Volumes are quoted in Dth/d. Industry convention treats Dth ≈ Mcf for feedgas;
we divide by 1000 to convert MCF/day -> MMcf/day.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError, ScraperError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

BASE_URL = "https://pipeline2.kindermorgan.com/Capacity/OpAvailPoint.aspx"

CYCLE_TO_KMI = {
    "timely": "TIMELY",
    "evening": "EVENING",
    "intraday1": "INTRADAY 1",
    "intraday2": "INTRADAY 2",
    "intraday3": "INTRADAY 3",
    "confirmed": "BEST AVAILABLE",
}

# Row index where the data-grid header lives inside the downloaded .xlsx
EXCEL_HEADER_ROW = 3

REQUIRED_COLUMNS = (
    "Loc",
    "Loc Name",
    "Total Scheduled Quantity",
    "Flow Ind",
)


class KMIScraper(BaseScraper):
    name = "kmi"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        by_code: dict[str, list[MeterPoint]] = {}
        for mp in ctx.meter_points:
            by_code.setdefault(mp.pipeline_code, []).append(mp)

        records: list[FlowRecord] = []
        for code, meters in by_code.items():
            try:
                xls_bytes, source_url = self._download_with_retry(ctx, code)
            except (ScraperError, Exception) as exc:   # noqa: BLE001
                log.error("KMI %s: download failed after retries: %s", code, exc)
                continue
            try:
                df = _read_xlsx(xls_bytes)
                log.info("KMI %s: %d rows in downloaded grid", code, len(df))
                records.extend(_match_meters(df, meters, ctx, source_url, code))
            except ParseError as exc:
                log.error("KMI %s: parse failed: %s", code, exc)
            self._sleep(ctx)
        return records

    def _download_with_retry(self, ctx: ScrapeContext, code: str) -> tuple[bytes, str]:
        """Download with retry + backoff (uses BaseScraper.with_retry).

        The KM portal intermittently drops connections (RemoteDisconnected) or
        rate-limits, especially after repeated hits from one IP. with_retry resets
        the session between attempts so flagged/sticky cookies are cleared.
        """
        return self.with_retry(lambda: self._download_xlsx(ctx, code),
                               label=f"KMI {code}")

    def _download_xlsx(self, ctx: ScrapeContext, code: str) -> tuple[bytes, str]:
        params = {
            "code": code,
            "flow_day": ctx.gas_day.strftime("%m/%d/%Y"),
            "cycle": CYCLE_TO_KMI[ctx.cycle],
            "type": "D",
        }
        log.info("KMI GET form: code=%s day=%s cycle=%s", code, ctx.gas_day, ctx.cycle)
        r1 = self.session.get(BASE_URL, params=params, timeout=30)
        r1.raise_for_status()
        soup = BeautifulSoup(r1.text, "lxml")

        form_data: dict[str, str] = {
            h.get("name"): h.get("value", "")
            for h in soup.find_all("input", {"type": "hidden"})
            if h.get("name")
        }
        download_name = None
        for inp in soup.find_all("input"):
            if "btnDownload" in (inp.get("name") or ""):
                download_name = inp.get("name")
                break
        if not download_name:
            raise ParseError("KMI: btnDownload not found on form page")
        form_data[f"{download_name}.x"] = "1"
        form_data[f"{download_name}.y"] = "1"
        form_data["ctl00$hdnIsDownload"] = "true"

        log.info("KMI POST download: code=%s", code)
        r2 = self.session.post(r1.url, data=form_data, timeout=120)
        r2.raise_for_status()

        ct = r2.headers.get("Content-Type", "")
        if "excel" not in ct.lower() and "sheet" not in ct.lower():
            raise ScraperError(f"KMI {code}: unexpected Content-Type {ct!r}; expected Excel")
        return r2.content, r2.url


def _read_xlsx(xls_bytes: bytes) -> pd.DataFrame:
    try:
        df = pd.read_excel(io.BytesIO(xls_bytes), header=EXCEL_HEADER_ROW)
    except Exception as exc:   # noqa: BLE001
        raise ParseError(f"KMI: failed to read xlsx: {exc}") from exc

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ParseError(f"KMI: missing required columns in xlsx: {missing} (got {list(df.columns)})")

    df = df.dropna(subset=["Loc"]).copy()
    df["Loc_str"] = df["Loc"].astype(str).str.strip()
    df = df[df["Loc_str"].str.match(r"^\d+$")]   # keep numeric Loc IDs only
    return df


def _match_meters(
    df: pd.DataFrame,
    meters: list[MeterPoint],
    ctx: ScrapeContext,
    source_url: str,
    code: str,
) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    for mp in meters:
        match = _row_match(df, mp)
        if match is None or match.empty:
            log.warning(
                "KMI: no row matched terminal=%s pipeline=%s code=%s meter_id=%r location_name=%r",
                mp.terminal, mp.pipeline, code, mp.meter_id, mp.location_name,
            )
            continue
        tsq = _parse_number(match["Total Scheduled Quantity"])
        if tsq is None:
            log.warning("KMI: unparseable TSQ for terminal=%s row=%r", mp.terminal, dict(match))
            continue
        mmcfd = tsq / 1000.0   # Dth/d -> MMcf/d
        direction = _direction_from_flow_ind(str(match.get("Flow Ind", ""))) or mp.direction
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
        log.info(
            "KMI: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["Loc_str"], match["Loc Name"], mmcfd, match.get("Flow Ind"),
        )
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint) -> pd.Series | None:
    if mp.meter_id:
        hits = df[df["Loc_str"] == str(mp.meter_id).strip()]
        if not hits.empty:
            return hits.iloc[0]
    needle = mp.location_name.lower()
    hits = df[df["Loc Name"].astype(str).str.lower().str.contains(needle, regex=False, na=False)]
    if not hits.empty:
        return hits.iloc[0]
    return None


_NUMBER_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _parse_number(s: Any) -> float | None:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    text = str(s).replace(",", "").strip()
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _direction_from_flow_ind(flow: str) -> Direction | None:
    f = flow.strip().upper()
    if f == "D":
        return "delivery"
    if f == "R":
        return "receipt"
    if f == "BD":
        # Bi-directional — treat as delivery off the pipe (feedgas)
        return "delivery"
    return None
