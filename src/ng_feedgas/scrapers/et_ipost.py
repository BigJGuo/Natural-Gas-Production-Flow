"""Energy Transfer iPost (multi-asset) scraper.

Generalizes the original `et_tgc` (Trunkline-only) scraper to all interstate
Energy Transfer assets that publish operationally-available capacity. Each
ET interstate exposes the same CSV endpoint shape; the asset is selected via
the `asset=` query string.

Endpoint pattern (verified 2026-05-27 — any ET subdomain works as a proxy):
  https://twtransfer.energytransfer.com/ipost/capacity/operationally-available-by-location
    ?asset={ASSET}&f=csv&extension=csv

Supported assets (per probe 2026-05-27):
  TGC    — Trunkline (Louisiana/Indiana/Mississippi)
  TW     — Transwestern (NM/AZ/CA)
  FEP    — Fayetteville Express (AR)
  PEPL   — Panhandle Eastern (TX/OK to MI)
  FGT    — Florida Gas Transmission (TX to FL; Energy Transfer 50% interest)

Not supported (Texas intrastates — design capacity only, no daily OAC):
  TPP    — Trans-Pecos (Waha → Presidio Mexico border)
  CTP    — Comanche Trail (Waha → San Elizario Mexico border)
  RGN    — RIGEL? (intrastate)

The CSV column conventions vary slightly between TGC (no Flow Ind on first
ordering) and TW/FEP/PEPL (Flow Ind in 6th column). We normalize by name.

Volumes are in Dth/d. Divide by 1000 for MMcf/d.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

import pandas as pd

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError, ScraperError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

BASE = "https://twtransfer.energytransfer.com"
CSV_URL_TMPL = BASE + "/ipost/capacity/operationally-available-by-location?asset={asset}&f=csv&extension=csv"
LANDING_TMPL = BASE + "/ipost/main/index?asset={asset}"

# Mapping from MeterPoint.pipeline_code -> ET asset query string
SUPPORTED_ASSETS = {"TGC", "TW", "FEP", "PEPL", "FGT"}


class EnergyTransferIPostScraper(BaseScraper):
    name = "et_ipost"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        return self.with_retry(lambda: self._fetch_impl(ctx))

    def _fetch_impl(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        by_asset: dict[str, list[MeterPoint]] = {}
        for mp in ctx.meter_points:
            code = mp.pipeline_code.upper()
            if code not in SUPPORTED_ASSETS:
                log.warning("ET-iPost: unsupported asset code %r for terminal=%s — "
                            "expected one of %s", code, mp.terminal, sorted(SUPPORTED_ASSETS))
                continue
            by_asset.setdefault(code, []).append(mp)

        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )

        records: list[FlowRecord] = []
        for asset, meters in by_asset.items():
            log.info("ET-iPost %s: GET landing then CSV", asset)
            self.session.get(LANDING_TMPL.format(asset=asset), timeout=30)
            r = self.session.get(CSV_URL_TMPL.format(asset=asset), timeout=60)
            r.raise_for_status()
            ct = (r.headers.get("Content-Type") or "").lower()
            if "csv" not in ct and "octet-stream" not in ct:
                log.error("ET-iPost %s: unexpected Content-Type %r", asset, ct)
                continue
            try:
                df = pd.read_csv(io.BytesIO(r.content))
            except Exception as exc:
                log.error("ET-iPost %s: failed to parse CSV: %s", asset, exc)
                continue
            df.columns = [c.strip() for c in df.columns]
            required = ["Loc", "Loc Name", "TSQ", "Flow Ind"]
            missing = [c for c in required if c not in df.columns]
            if missing:
                log.error("ET-iPost %s: missing columns %s; got %s",
                          asset, missing, list(df.columns))
                continue
            df["Loc"] = df["Loc"].astype(str).str.strip()
            log.info("ET-iPost %s: parsed %d rows", asset, len(df))
            records.extend(_match_meters(df, meters, ctx, r.url))
            self._sleep(ctx)
        return records


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
                "ET-iPost: no row matched terminal=%s pipeline_code=%s meter_id=%r location_name=%r",
                mp.terminal, mp.pipeline_code, mp.meter_id, mp.location_name,
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
        log.info(
            "ET-iPost: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["Loc"], match["Loc Name"], mmcfd, match["Flow Ind"],
        )
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint):
    if mp.meter_id:
        hits = df[df["Loc"] == str(mp.meter_id).strip()]
        if not hits.empty:
            for _, r in hits.iterrows():
                if (str(r["Flow Ind"]) or "").strip().upper().startswith(mp.direction[0].upper()):
                    return r
            return hits.iloc[0]
    needle = mp.location_name.lower()
    hits = df[df["Loc Name"].astype(str).str.lower().str.contains(needle, regex=False, na=False)]
    if not hits.empty:
        return hits.iloc[0]
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


def _dir(f: str) -> Direction | None:
    f_up = (f or "").strip().upper()
    if f_up == "D":
        return "delivery"
    if f_up == "R":
        return "receipt"
    if f_up == "BD":
        return "delivery"
    return None
