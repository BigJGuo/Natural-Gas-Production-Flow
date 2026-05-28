"""TC Energy eConnects (`ebb.tceconnects.com`) scraper.

This portal serves multiple TC Energy pipelines (ANR, Columbia Gas Transmission/TCO,
Columbia Gulf, Crossroads, Hardy Storage, Millennium, Northern Border, TC LA Intrastate)
through a JavaScript-driven SQL Server Reporting Services (SSRS) interface. The
landing page menu uses `changeAsset()` JS to switch pipelines and `LaunchReport()` to
open ReportViewer.aspx in an iframe; the CSV export button triggers SSRS's
`$find('ReportViewer1').exportReport('CSV')` which requires a real browser.

So this scraper uses Playwright (headless Chromium) to:
  1. Open the landing page with assetid set
  2. Trigger changeAsset() to confirm the asset context
  3. Navigate to ReportViewer.aspx for the desired report
  4. Wait for SSRS to render
  5. Trigger exportReport('CSV') and capture the download
  6. Parse with pandas

Each MeterPoint must declare `pipeline_code` matching one of the supported asset
labels below — the scraper picks the right asset id and report path:

  ANR   -> asset 3005, report OperationallyAvailableCapacityANR
  TCO   -> asset 51,   report OperationallyAvailableCapacity
  CGT   -> asset 14,   report OperationallyAvailableCapacity   (Columbia Gulf)

Two CSV column conventions are normalized: TCO/CGT use `TotalScheduledQuantity`,
ANR uses `TotalSchedQty`. Volumes are in Dth/d; divide by 1000 for MMcf/d.

Note: this scraper opens a new Chromium browser per `fetch()` call. For a single
gas day, fetching all three assets takes about 30 seconds.
"""
from __future__ import annotations

import io
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError, ScraperError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

# pipeline_code -> (asset_id, asset_label, report_path)
ASSETS: dict[str, tuple[int, str, str]] = {
    "ANR": (3005, "ANR Pipeline Company (ANR)", "OperationallyAvailableCapacityANR"),
    "TCO": (51,   "Columbia Gas Transmission (TCO)", "OperationallyAvailableCapacity"),
    "CGT": (14,   "Columbia Gulf Transmission (CGT)", "OperationallyAvailableCapacity"),
}

# CSV column candidates per asset family
TSQ_COLUMN_CANDIDATES = ["TotalScheduledQuantity", "TotalSchedQty"]


class TCeConnectsScraper(BaseScraper):
    name = "tceconnects"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        # Group meters by pipeline_code (asset)
        by_code: dict[str, list[MeterPoint]] = {}
        for mp in ctx.meter_points:
            by_code.setdefault(mp.pipeline_code, []).append(mp)

        records: list[FlowRecord] = []
        try:
            from playwright.sync_api import sync_playwright  # local import — heavy
        except ImportError as exc:
            raise ScraperError(
                "Playwright not installed. Run: pip install playwright && python -m playwright install chromium"
            ) from exc

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for code, meters in by_code.items():
                    if code not in ASSETS:
                        log.error("TCeConnects: unsupported pipeline_code %r (expected %s)",
                                  code, list(ASSETS))
                        continue
                    try:
                        df, source_url = self._download_csv(browser, code)
                    except Exception as exc:   # noqa: BLE001
                        log.error("TCeConnects %s: download failed: %s", code, exc)
                        continue
                    log.info("TCeConnects %s: parsed %d rows", code, len(df))
                    records.extend(_match_meters(df, meters, ctx, source_url))
                    self._sleep(ctx)
            finally:
                browser.close()
        return records

    def _download_csv(self, browser, code: str) -> tuple[pd.DataFrame, str]:
        asset_id, asset_label, report_path = ASSETS[code]
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        log.info("TCeConnects %s: loading landing", code)
        page.goto(f"https://ebb.tceconnects.com/infopost/Default.aspx?assetid={asset_id}",
                  wait_until="networkidle", timeout=60000)
        page.evaluate(f"changeAsset({asset_id!r}, {asset_label!r})")
        page.wait_for_load_state("networkidle", timeout=30000)

        report_url = (
            f"https://ebb.tceconnects.com/infopost/ReportViewer.aspx"
            f"?/InfoPost/{report_path}&pAssetNbr={asset_id}"
        )
        log.info("TCeConnects %s: loading report viewer", code)
        page.goto(report_url, wait_until="networkidle", timeout=120000)
        page.wait_for_timeout(5000)  # let SSRS finish rendering

        log.info("TCeConnects %s: triggering CSV export", code)
        with page.expect_download(timeout=120000) as dl_info:
            page.evaluate("$find('ReportViewer1').exportReport('CSV')")
        dl = dl_info.value

        # Save to a temp file then read with pandas
        tmp = Path(tempfile.gettempdir()) / f"tceconnects_{code}_{asset_id}.csv"
        dl.save_as(str(tmp))
        try:
            df = pd.read_csv(tmp)
        except Exception as exc:
            raise ParseError(f"TCeConnects {code}: failed to parse CSV: {exc}") from exc
        finally:
            try: tmp.unlink()
            except OSError: pass

        # Normalize TSQ column name
        tsq_col = next((c for c in TSQ_COLUMN_CANDIDATES if c in df.columns), None)
        if tsq_col is None:
            raise ParseError(
                f"TCeConnects {code}: no TSQ column found. Columns: {list(df.columns)}"
            )
        if tsq_col != "TotalScheduledQuantity":
            df = df.rename(columns={tsq_col: "TotalScheduledQuantity"})
        if "Location" not in df.columns or "LocationName" not in df.columns or "FlowInd" not in df.columns:
            raise ParseError(
                f"TCeConnects {code}: missing required columns. Got: {list(df.columns)}"
            )

        df["Location"] = df["Location"].astype(str).str.strip()
        return df, report_url


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
                "TCeConnects: no row matched terminal=%s pipeline_code=%s meter_id=%r location_name=%r",
                mp.terminal, mp.pipeline_code, mp.meter_id, mp.location_name,
            )
            continue
        tsq = _parse_number(match["TotalScheduledQuantity"])
        if tsq is None:
            continue
        mmcfd = tsq / 1000.0
        direction = _dir_from_flow(str(match.get("FlowInd", ""))) or mp.direction
        out.append(FlowRecord(
            gas_day=ctx.gas_day,
            cycle=ctx.cycle,
            terminal=mp.terminal,
            pipeline=mp.pipeline,
            meter_point=str(match["LocationName"]).strip(),
            mmcfd=mmcfd,
            direction=direction,  # type: ignore[arg-type]
            source_url=source_url,
            scraped_at=now,
        ))
        log.info(
            "TCeConnects: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["Location"], match["LocationName"], mmcfd, match["FlowInd"],
        )
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint):
    def _highest_tsq(hits: pd.DataFrame):
        tsq = pd.to_numeric(
            hits["TotalScheduledQuantity"].astype(str).str.replace(",", ""),
            errors="coerce",
        ).fillna(0)
        return hits.iloc[tsq.values.argmax()]

    if mp.meter_id:
        hits = df[df["Location"] == str(mp.meter_id).strip()]
        if not hits.empty:
            # Prefer rows in the expected direction (or BD); among those, the
            # highest-TSQ row. A Loc can appear multiple times (e.g. a 0-TSQ
            # stale row plus the active one) — never just take the first.
            want = mp.direction[0].upper()
            dir_hits = hits[hits["FlowInd"].astype(str).str.strip().str.upper().isin(
                {want, "BD"} if mp.direction == "delivery" else {want})]
            return _highest_tsq(dir_hits if not dir_hits.empty else hits)
    needle = mp.location_name.lower()
    hits = df[df["LocationName"].astype(str).str.lower().str.contains(needle, regex=False, na=False)]
    if not hits.empty:
        return _highest_tsq(hits)
    return None


_NUMBER_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _parse_number(s) -> float | None:
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


def _dir_from_flow(f: str) -> Direction | None:
    f_up = (f or "").strip().upper()
    if f_up == "D":
        return "delivery"
    if f_up == "R":
        return "receipt"
    if f_up == "BD":
        return "delivery"
    return None
