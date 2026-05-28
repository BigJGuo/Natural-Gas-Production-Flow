"""ng_feedgas CLI — `python -m ng_feedgas <command>`."""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import click

from .analysis.prompt import write_prompt
from .analysis.stats import compute
from .calibration import ais as ais_mod
from .calibration import eia as eia_mod
from .config import Config, MeterPoint, load_config
from .models import FlowRecord
from .scrapers import SCRAPERS, BaseScraper, ScrapeContext
from .storage.db import DEFAULT_DB_PATH, connect, terminal_totals, upsert_flows
from .storage.export import render_part4_text, write_part4_csv
from .validators import validate

EXPORTS_DIR = Path(__file__).resolve().parents[2] / "data" / "exports"


def _parse_date(s: str) -> date:
    if s.lower() in ("today",):
        return date.today()
    if s.lower() in ("yesterday",):
        return date.today() - timedelta(days=1)
    return datetime.strptime(s, "%Y-%m-%d").date()


def _build_scrapers(cfg: Config, only: str) -> dict[BaseScraper, list[MeterPoint]]:
    if only == "all":
        names = list(SCRAPERS.keys())
    else:
        names = [only]
    out: dict[BaseScraper, list[MeterPoint]] = {}
    for name in names:
        cls = SCRAPERS.get(name)
        if not cls:
            raise click.UsageError(f"Unknown scraper: {name}. Options: {list(SCRAPERS)}")
        meters = cfg.for_scraper(name)
        if not meters:
            click.echo(f"  (skipping {name}: no meter points configured)", err=True)
            continue
        out[cls()] = meters
    return out


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
def cli(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )


@cli.command()
@click.option("--date", "date_s", default="today", help='Gas day. "today", "yesterday", or YYYY-MM-DD.')
@click.option("--cycle", default="evening",
              type=click.Choice(["timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"]),
              show_default=True)
@click.option("--pipeline", "scraper_only", default="all",
              type=click.Choice([
                  "kmi", "williams", "williams_nwp", "tcenergy",
                  "enbridge", "et_tgc", "et_ipost", "tceconnects",
                  "iroquois", "all",
              ]),
              show_default=True, help="Run one scraper or all.")
@click.option("--delay", "request_delay_s", default=2.5, type=float, show_default=True,
              help="Per-request sleep between HTTP calls (seconds).")
def pull(date_s: str, cycle: str, scraper_only: str, request_delay_s: float) -> None:
    """Scrape pipeline EBBs and upsert flows into SQLite."""
    cfg = load_config()
    gas_day = _parse_date(date_s)
    click.echo(f"Pulling {scraper_only} scrapers for {gas_day} cycle={cycle}")

    scrapers = _build_scrapers(cfg, scraper_only)
    total_inserted = 0
    with connect() as conn:
        for scraper, meters in scrapers.items():
            click.echo(f"  -> {scraper.name}: {len(meters)} meter points")
            ctx = ScrapeContext(
                gas_day=gas_day, cycle=cycle,  # type: ignore[arg-type]
                meter_points=meters, request_delay_s=request_delay_s,
            )
            try:
                records: list[FlowRecord] = scraper.fetch(ctx)
            except Exception as exc:   # noqa: BLE001 — isolate per-scraper failures
                click.echo(f"     ! {scraper.name} failed: {exc}", err=True)
                continue
            inserted = upsert_flows(conn, records)
            total_inserted += inserted
            click.echo(f"     ok: {inserted} rows upserted")

        # Run validators against whatever ended up in the DB for this day/cycle
        totals = terminal_totals(conn, gas_day, cycle)
        for issue in validate(cfg, totals):
            click.echo(f"  ! {issue.severity}: {issue.message}", err=True)

    click.echo(f"Done. {total_inserted} total rows written to {DEFAULT_DB_PATH}.")


@cli.command()
@click.option("--date", "date_s", default="today")
@click.option("--cycle", default="evening",
              type=click.Choice(["timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"]))
def report(date_s: str, cycle: str) -> None:
    """Read SQLite, write Part-4 CSV and Part-5 prompt to data/exports/."""
    gas_day = _parse_date(date_s)
    csv_path = EXPORTS_DIR / f"{gas_day.isoformat()}_{cycle}.csv"
    prompt_path = EXPORTS_DIR / f"{gas_day.isoformat()}_{cycle}_prompt.txt"

    with connect() as conn:
        write_part4_csv(conn, gas_day, cycle, csv_path)
        stats = compute(conn, gas_day, cycle)
        write_prompt(stats, prompt_path)

    click.echo(f"Wrote CSV:    {csv_path}")
    click.echo(f"Wrote prompt: {prompt_path}")
    click.echo(f"U.S. total ({cycle}, {gas_day}): {stats.us_total:,.0f} MMcf/d")


@cli.command()
@click.option("--date", "date_s", default="today")
@click.option("--cycle", default="evening",
              type=click.Choice(["timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"]))
def show(date_s: str, cycle: str) -> None:
    """Print the Part-4 entry table to stdout."""
    gas_day = _parse_date(date_s)
    with connect() as conn:
        click.echo(render_part4_text(conn, gas_day, cycle))


@cli.command(name="eia-fetch")
@click.option("--weeks", default=52, type=int, show_default=True,
              help="How many weeks of EIA history to fetch.")
def eia_fetch(weeks: int) -> None:
    """Fetch EIA weekly LNG export totals (calibration baseline).

    Requires EIA_API_KEY env var (free at https://www.eia.gov/opendata/register.php).
    """
    try:
        df = eia_mod.fetch_weekly_lng_exports(n_weeks=weeks)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with connect() as conn:
        # Ensure the calibration schema is loaded
        from .calibration.ais import _ensure_schema
        _ensure_schema(conn)
        n = eia_mod.upsert_to_db(conn, df)
    click.echo(f"EIA: upserted {n} weekly rows. Latest week ending: {df['period'].iloc[-1]}, "
               f"{df['us_lng_bcfd'].iloc[-1]:.2f} Bcf/d")


@cli.command(name="ais-collect")
@click.option("--duration", default=60, type=int, show_default=True,
              help="Seconds to listen on the AIS stream.")
def ais_collect(duration: int) -> None:
    """Listen to aisstream.io for vessel activity around LNG terminals.

    Requires AISSTREAM_API_KEY env var (free at https://aisstream.io).
    Run this on a schedule (e.g. every hour) to build up an observation history.
    """
    try:
        n = ais_mod.collect(duration_s=duration)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"AIS: collected {n} observations in {duration}s")


@cli.command(name="ais-infer")
@click.option("--date", "date_s", default="today")
def ais_infer(date_s: str) -> None:
    """Roll AIS observations into per-terminal daily feedgas estimates."""
    gas_day = _parse_date(date_s)
    with connect() as conn:
        out = ais_mod.infer_daily_activity(gas_day, conn)
    if out.empty:
        click.echo("(no AIS rows)")
        return
    for _, row in out.iterrows():
        click.echo(
            f"  {row['terminal']:<16s}  ships_seen={row['ship_at_berth']}  "
            f"moored_hours={row['moored_hours']:.1f}  est={row['est_feedgas_mmcfd']:.0f} MMcf/d"
        )


def main() -> None:
    cli(prog_name="ng_feedgas")   # pragma: no cover


if __name__ == "__main__":
    main()
