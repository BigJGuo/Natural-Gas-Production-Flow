"""gasnom.com EBB scraper — Golden Pass Pipeline + Cameron Interstate Pipeline.

gasnom.com hosts several pipelines' FERC informational postings under
  https://www.gasnom.com/ip/{slug}/oauc.cfm?type=1&dt=MM/DD/YYYY
as a ColdFusion-rendered HTML table (no CSV export — we parse the table). The
Operationally-Available-Capacity posting ("type=1") carries a TSQ (Total
Scheduled Quantity) column per location; the LNG-plant delivery meter's TSQ is
the feedgas we want. Verified 2026-05-29: plain HTTP works (no Imperva block).

Cycle: gasnom posts the Evening cycle (effective ~09:00 CT). The page exposes a
gas-day selector (?dt=MM/DD/YYYY), which we honor; there is no public per-cycle
selector, so the posting is effectively the Evening cycle for the requested day.

Volumes are in Dth/d — divide by 1000 for MMcf/d.

Supported assets (MeterPoint.pipeline_code -> gasnom URL slug):
  GPPL -> goldenpass   Golden Pass Pipeline  -> Golden Pass LNG (Loc 1097217)
  CIP  -> cameron      Cameron Interstate    -> Cameron LNG     (Loc 772300)
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

URL_TMPL = "https://www.gasnom.com/ip/{slug}/oauc.cfm?type=1&dt={dt}"

# MeterPoint.pipeline_code -> gasnom URL slug
ASSET_SLUGS = {"GPPL": "goldenpass", "CIP": "cameron"}


class GasNomScraper(BaseScraper):
    name = "gasnom"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        return self.with_retry(lambda: self._fetch_impl(ctx))

    def _fetch_impl(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        by_slug: dict[str, list[MeterPoint]] = {}
        for mp in ctx.meter_points:
            slug = ASSET_SLUGS.get(mp.pipeline_code.upper())
            if not slug:
                log.warning("gasnom: unknown pipeline_code %r for terminal=%s "
                            "(known: %s)", mp.pipeline_code, mp.terminal,
                            sorted(ASSET_SLUGS))
                continue
            by_slug.setdefault(slug, []).append(mp)

        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        dt = ctx.gas_day.strftime("%m/%d/%Y")

        records: list[FlowRecord] = []
        for slug, meters in by_slug.items():
            url = URL_TMPL.format(slug=slug, dt=dt)
            log.info("gasnom %s: GET %s", slug, url)
            r = self.session.get(url, timeout=60)
            r.raise_for_status()
            df = _parse_oac_table(r.text, slug)
            log.info("gasnom %s: parsed %d location rows", slug, len(df))
            records.extend(_match_meters(df, meters, ctx, r.url))
            self._sleep(ctx)
        return records


def _parse_oac_table(html: str, slug: str) -> pd.DataFrame:
    """Locate the OAC data table and return it with header row applied."""
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError as exc:
        raise ParseError(f"gasnom {slug}: no tables in OAC page: {exc}") from exc
    for t in tables:
        header = [str(c).strip() for c in t.iloc[0].tolist()]
        if {"Loc", "Flow Ind", "TSQ"}.issubset(set(header)):
            df = t.copy()
            df.columns = header
            df = df.iloc[1:].reset_index(drop=True).dropna(how="all")
            loc = df["Loc"].astype(str).str.strip().str.lower()
            return df[(loc != "") & (loc != "nan")]
    raise ParseError(
        f"gasnom {slug}: OAC data table (Loc/Flow Ind/TSQ columns) not found")


def _match_meters(df: pd.DataFrame, meters: list[MeterPoint],
                  ctx: ScrapeContext, source_url: str) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    for mp in meters:
        match = _row_match(df, mp)
        if match is None:
            log.warning("gasnom: no row matched terminal=%s pipeline_code=%s "
                        "meter_id=%r location_name=%r", mp.terminal,
                        mp.pipeline_code, mp.meter_id, mp.location_name)
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
            meter_point=str(match["Location Name"]).strip(),
            mmcfd=mmcfd,
            direction=direction,  # type: ignore[arg-type]
            source_url=source_url,
            scraped_at=now,
        ))
        log.info("gasnom: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
                 mp.terminal, match["Loc"], match["Location Name"], mmcfd,
                 match.get("Flow Ind"))
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint):
    if mp.meter_id:
        hits = df[df["Loc"].astype(str).str.strip() == str(mp.meter_id).strip()]
        if not hits.empty:
            # Same Loc can post both a Delivery and a Receipt row — prefer the
            # one whose Flow Ind matches the configured direction.
            for _, r in hits.iterrows():
                if (str(r.get("Flow Ind", "")) or "").strip().upper().startswith(
                        mp.direction[0].upper()):
                    return r
            return hits.iloc[0]
    needle = mp.location_name.lower()
    hits = df[df["Location Name"].astype(str).str.lower().str.contains(
        needle, regex=False, na=False)]
    return hits.iloc[0] if not hits.empty else None


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
    if f_up.startswith("D"):
        return "delivery"
    if f_up.startswith("R"):
        return "receipt"
    return None
