"""Energy Transfer — TGC Messenger (Trunkline / TGC) scraper.

Portal:  https://tgcmessenger.energytransfer.com/ipost/main/index?asset=TGC

Public CSV endpoint:
  /ipost/capacity/operationally-available-by-location?asset=TGC&f=csv&extension=csv

Returns a CSV with columns: Loc, Loc Name, Loc Purp Desc, Loc/QTI, DC (Design Capacity),
OPC (Operating Capacity), TSQ (Total Scheduled Quantity), OAC, Loc Zn, IT, Flow Ind,
State, County, Operator, OBA, G/T, Miles, EGM, Flw Cntl, All Qty Avail, Qty Reason.

Note: this endpoint returns the MOST-RECENTLY-POSTED snapshot — it does not take a
date or cycle parameter directly. For historical days, manual selection on the page
form is required (not yet implemented).

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

LANDING = "https://tgcmessenger.energytransfer.com/ipost/main/index?asset=TGC"
CSV_URL = "https://tgcmessenger.energytransfer.com/ipost/capacity/operationally-available-by-location?asset=TGC&f=csv&extension=csv"


class EnergyTransferTGCScraper(BaseScraper):
    name = "et_tgc"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        log.info("ET-TGC: GET landing then CSV")
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        self.session.get(LANDING, timeout=30)
        r = self.session.get(CSV_URL, timeout=60)
        r.raise_for_status()
        if "csv" not in (r.headers.get("Content-Type") or "").lower():
            raise ScraperError(f"ET-TGC: unexpected Content-Type {r.headers.get('Content-Type')!r}")

        try:
            df = pd.read_csv(io.BytesIO(r.content))
        except Exception as exc:
            raise ParseError(f"ET-TGC: failed to parse CSV: {exc}") from exc

        # Normalize column names (the CSV pads columns with trailing whitespace)
        df.columns = [c.strip() for c in df.columns]
        required = ["Loc", "Loc Name", "TSQ", "Flow Ind"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ParseError(f"ET-TGC: missing columns {missing}; got {list(df.columns)}")

        df["Loc"] = df["Loc"].astype(str).str.strip()
        log.info("ET-TGC: parsed %d rows", len(df))
        return _match_meters(df, ctx, r.url)


def _match_meters(df: pd.DataFrame, ctx: ScrapeContext, source_url: str) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    for mp in ctx.meter_points:
        match = _row_match(df, mp)
        if match is None:
            log.warning(
                "ET-TGC: no row matched terminal=%s meter_id=%r location_name=%r",
                mp.terminal, mp.meter_id, mp.location_name,
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
            "ET-TGC: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["Loc"], match["Loc Name"], mmcfd, match["Flow Ind"],
        )
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint):
    if mp.meter_id:
        hits = df[df["Loc"] == str(mp.meter_id).strip()]
        if not hits.empty:
            # Prefer matching direction
            for _, r in hits.iterrows():
                if (r["Flow Ind"] or "").strip().upper().startswith(mp.direction[0].upper()):
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
