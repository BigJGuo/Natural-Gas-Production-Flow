"""Enbridge — Texas Eastern Transmission (TETCO) scraper.

Portal:  https://rtba.enbridge.com/InformationalPosting/Default.aspx?bu=TE&Type=OA

The page is ASP.NET WebForms with __VIEWSTATE. A "Downloadable Format" link triggers
a __doPostBack on `ctl00$MainContent$ctl01$oaDefault$hlDown$LinkButton1` which returns
the OAC report for the selected cycle as a CSV.

Cycle handling: TETCO posts multiple snapshots per gas day under names like
TIMELY_YYYY-MM-DD_HHMM, LATE_YYYY-MM-DD_HHMM, LATEC_YYYY-MM-DD_HHMM,
INTRDY_YYYY-MM-DD_HHMM, INTRDYC_YYYY-MM-DD_HHMM. The dropdown's first option
is the most recent. We map our generic cycle names to a TETCO prefix and pick
the most-recent matching option.

CSV columns include: Loc, Loc_Name, Flow_Ind_Desc, Total_Scheduled_Quantity, etc.
Quantities are in MMBtu/d (Meas_Basis_Desc column confirms). Industry convention
treats MMBtu/d ≈ Mcf/d for feedgas, so divide by 1000 for MMcf/d.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

import pandas as pd
from bs4 import BeautifulSoup

from ..config import MeterPoint
from ..models import Direction, FlowRecord, ParseError, ScraperError
from .base import BaseScraper, ScrapeContext

log = logging.getLogger(__name__)

FORM_URL = "https://rtba.enbridge.com/InformationalPosting/Default.aspx?bu=TE&Type=OA"

# Map our canonical cycle names to TETCO option-value prefixes
CYCLE_TO_TETCO_PREFIX = {
    "timely": "TIMELY",
    "evening": "LATEC",      # Late Confirmed is the closest to "evening final scheduled"
    "intraday1": "INTRDY",
    "intraday2": "INTRDY",
    "intraday3": "INTRDY",
    "confirmed": "INTRDYC",  # Intraday Confirmed is the final-final
}

DROPDOWN_NAME = "ctl00$MainContent$ctl01$oaDefault$ucSelector$ddlSelector"
DOWNLOAD_TARGET = "ctl00$MainContent$ctl01$oaDefault$hlDown$LinkButton1"


def select_tetco_option(option_values: list[str], prefix: str,
                        gas_day_token: str) -> str | None:
    """Choose the TETCO dropdown option for the requested gas day.

    Priority: (1) the preferred cycle prefix for that day, (2) any snapshot for
    that day (first listed = most recent), (3) None. It deliberately NEVER falls
    back to the newest available option — the old code did `opts[0]`, which on
    the current portal (only prior-day TIMELY snapshots) silently stored
    wrong-day data. Returning None makes the caller treat the day as missing.
    Pure function so the regression is unit-testable.
    """
    for val in option_values:
        if val.startswith(f"{prefix}_") and gas_day_token in val:
            return val
    for val in option_values:
        if gas_day_token in val:
            return val
    return None


class EnbridgeTETCOScraper(BaseScraper):
    name = "enbridge_tetco"

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        return self.with_retry(lambda: self._fetch_impl(ctx))

    def _fetch_impl(self, ctx: ScrapeContext) -> list[FlowRecord]:
        if not ctx.meter_points:
            return []

        log.info("Enbridge TETCO: GET form for day=%s cycle=%s", ctx.gas_day, ctx.cycle)
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        r1 = self.session.get(FORM_URL, timeout=30)
        r1.raise_for_status()

        soup = BeautifulSoup(r1.text, "lxml")
        form = soup.find("form")
        if not form:
            raise ParseError("TETCO: form not found on OAC page")

        # Collect form fields
        form_data: dict[str, str] = {}
        for inp in form.find_all(["input", "select", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            if inp.get("type") in ("button", "submit", "image"):
                continue
            if inp.name == "select":
                sel = inp.find("option", selected=True) or inp.find("option")
                form_data[name] = sel.get("value", "") if sel else ""
            else:
                form_data[name] = inp.get("value", "")

        # Pick the right cycle option from the dropdown
        sel = form.find("select", {"name": DROPDOWN_NAME})
        if sel is None:
            raise ParseError("TETCO: cycle dropdown not found")
        opts = [(o.get("value", ""), o.get_text(strip=True))
                for o in sel.find_all("option") if o.get("value")]
        prefix = CYCLE_TO_TETCO_PREFIX.get(ctx.cycle)
        if not prefix:
            raise ScraperError(f"TETCO: unsupported cycle {ctx.cycle!r}")
        gas_day_token = ctx.gas_day.strftime("%Y-%m-%d")
        chosen = select_tetco_option([str(v) for v, _ in opts], prefix, gas_day_token)
        if not chosen:
            sample = [v for v, _ in opts[:5]]
            raise ParseError(
                f"TETCO: no snapshot for gas_day {gas_day_token}; the portal has "
                f"no option for that day (sample: {sample}). Treating as missing "
                f"rather than storing a wrong-day value."
            )
        form_data[DROPDOWN_NAME] = chosen
        log.info("TETCO: selected cycle option %s", chosen)

        # Set the date input fields (platform-safe format; %-m is Linux-only)
        form_data["ctl00$MainContent$ctl01$oaDefault$ucDate$rdpDate"] = ctx.gas_day.strftime("%Y-%m-%d")
        form_data["ctl00$MainContent$ctl01$oaDefault$ucDate$rdpDate$dateInput"] = (
            f"{ctx.gas_day.month}/{ctx.gas_day.day}/{ctx.gas_day.year}"
        )

        # Trigger the download via __doPostBack
        form_data["__EVENTTARGET"] = DOWNLOAD_TARGET
        form_data["__EVENTARGUMENT"] = ""

        log.info("TETCO: POST to trigger download")
        r2 = self.session.post(FORM_URL, data=form_data, timeout=120)
        r2.raise_for_status()

        ct = (r2.headers.get("Content-Type") or "").lower()
        if "csv" not in ct and "text/plain" not in ct:
            raise ScraperError(
                f"TETCO: unexpected Content-Type {ct!r} after download POST; "
                f"first 200 bytes: {r2.content[:200]!r}"
            )

        try:
            df = pd.read_csv(io.BytesIO(r2.content))
        except Exception as exc:
            raise ParseError(f"TETCO: failed to parse CSV: {exc}") from exc

        required = ["Loc", "Loc_Name", "Total_Scheduled_Quantity", "Flow_Ind_Desc"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ParseError(f"TETCO: missing columns {missing}; got {list(df.columns)}")

        df["Loc"] = df["Loc"].astype(str).str.strip()
        log.info("TETCO: parsed %d rows", len(df))
        return _match_meters(df, ctx, r2.url)


def _match_meters(df: pd.DataFrame, ctx: ScrapeContext, source_url: str) -> list[FlowRecord]:
    out: list[FlowRecord] = []
    now = datetime.now(timezone.utc)
    for mp in ctx.meter_points:
        match = _row_match(df, mp)
        if match is None:
            log.warning(
                "TETCO: no row matched terminal=%s meter_id=%r location_name=%r",
                mp.terminal, mp.meter_id, mp.location_name,
            )
            continue
        tsq = _parse_number(match["Total_Scheduled_Quantity"])
        if tsq is None:
            continue
        mmcfd = tsq / 1000.0
        direction = _dir_from_desc(str(match.get("Flow_Ind_Desc", ""))) or mp.direction
        out.append(FlowRecord(
            gas_day=ctx.gas_day,
            cycle=ctx.cycle,
            terminal=mp.terminal,
            pipeline=mp.pipeline,
            meter_point=str(match["Loc_Name"]).strip(),
            mmcfd=mmcfd,
            direction=direction,  # type: ignore[arg-type]
            source_url=source_url,
            scraped_at=now,
        ))
        log.info(
            "TETCO: matched terminal=%s loc=%s name=%r mmcfd=%.1f flow=%s",
            mp.terminal, match["Loc"], match["Loc_Name"], mmcfd, match["Flow_Ind_Desc"],
        )
    return out


def _row_match(df: pd.DataFrame, mp: MeterPoint):
    if mp.meter_id:
        hits = df[df["Loc"] == str(mp.meter_id).strip()]
        # Prefer the row matching expected direction
        if not hits.empty:
            for _, r in hits.iterrows():
                if mp.direction.lower() in str(r["Flow_Ind_Desc"]).lower():
                    return r
            return hits.iloc[0]
    needle = mp.location_name.lower()
    hits = df[df["Loc_Name"].astype(str).str.lower().str.contains(needle, regex=False, na=False)]
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


def _dir_from_desc(d: str) -> Direction | None:
    d_low = (d or "").lower()
    if "delivery" in d_low:
        return "delivery"
    if "receipt" in d_low:
        return "receipt"
    return None
