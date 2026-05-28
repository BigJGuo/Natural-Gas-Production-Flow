# ng_feedgas — U.S. LNG Feedgas + Cross-Border Gas Flow Tracker

Scrapes scheduled-quantity data from public pipeline EBB (Electronic Bulletin Board)
portals, normalizes everything to a canonical `FlowRecord`, stores history in SQLite,
runs reasonableness validators, and renders a Plotly Dash dashboard. Covers U.S. LNG
feedgas, U.S.→Mexico pipeline exports, and U.S.↔Canada border crossings.

> **Status:** Operational. 9 scrapers across ~20 terminals/crossings. Pipeline data
> updates run on Windows Task Scheduler (daily + intraday). See `MASTERPLAN.md` for
> roadmap and `docs/portal_notes.md` for portal specifics.

## Data confidence — read this first

Not all numbers are equally trustworthy. The dashboard color-codes every terminal:

- **Exact (green)** — single-meter border crossings (the 7 Mexico EPNG/Sierrita points,
  Niagara, Sumas, Waddington). One meter = the entire physical flow at that point.
  These are faithful to the operator-posted scheduled quantities; use them as-is.
- **Lower bound (amber)** — LNG terminals are fed by *multiple* pipes, some private
  (e.g. Sabine's Creole Trail, Cameron's CIP, Corpus's CCPL). We sum the public
  pipelines only, so the per-terminal total is a floor. **Trust the day-over-day
  *change*, not the absolute level.** Captured U.S. LNG runs ~50% of the EIA monthly
  total — the gap is private/intrastate feeders and the two unscraped terminals.
- **Not captured (gray)** — Calcasieu Pass (private), Golden Pass (no scraper yet),
  Chippawa/Emerson (no scraper yet).

Calibrate the amber tier against the EIA monthly LNG total (loaded via `eia-fetch`).

## Scrapers

| Scraper | Portal | Pipelines | Tech |
|---|---|---|---|
| `kmi` | pipeline2.kindermorgan.com | NGPL, KMLP, EEC, SGP (Sierrita), EPNG, TGP | requests + viewstate POST + xlsx |
| `tcenergy` | (delegates to KMI, code=TGP) | TGP (Cameron, Plaquemines) | — |
| `williams` | 1line.williams.com | Transco (Freeport, Cove Point) | requests + 4-step JSP flow |
| `williams_nwp` | northwest.williams.com | Northwest Pipeline (Sumas) | requests + HTML grid |
| `enbridge` | rtba.enbridge.com | TETCO | requests + viewstate POST + CSV |
| `et_ipost` | *.energytransfer.com | TGC, TW, FEP, PEPL, FGT | requests + direct CSV |
| `tceconnects` | ebb.tceconnects.com | Columbia Gulf, TCO, ANR | **Playwright** (SSRS, JS-only) |
| `iroquois` | iol.iroquois.com | Iroquois (Waddington, Brookfield) | **Playwright** (ExtJS + Imperva) |

`--pipeline fast` runs the 7 HTTP scrapers; `--pipeline slow` runs the 2 Playwright ones.

> Playwright scrapers need: `pip install playwright && python -m playwright install chromium`

### `--date` support
`kmi`/`tcenergy`, `williams`, `enbridge` honor `--date`. `et_ipost`, `williams_nwp`,
`tceconnects`, `iroquois` only return the most-recent posted snapshot (their portals
expose no date selector), so historical backfill is not possible for those.

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium    # for tceconnects + iroquois
```

## Usage

```powershell
$env:PYTHONPATH = "src"
python -m ng_feedgas pull   --date today --cycle auto --pipeline all   # scrape
python -m ng_feedgas show   --date today --cycle evening               # Part-4 table
python -m ng_feedgas report --date today --cycle evening               # CSV + Part-5 prompt
python -m ng_feedgas changes --date today --cycle auto --only-alerts   # DoD/WoW/MoM deviations
```

`--cycle auto` picks the latest-posted cycle by U.S./Central clock. The Part-5 prompt
covers **U.S. LNG only** — Mexico/Canada are reported separately and never folded into
the LNG total (so the per-terminal lines always sum to the printed total).

## Dashboard

```powershell
python dashboard.py        # http://localhost:8050
```

Region tabs (All / U.S. LNG / Mexico / Canada), per-terminal bars with confidence
outlines, % of nameplate, deviation alerts (day/week/month), EIA overlay, and an AIS
vessel-detection table. Hit **↻ Reload Data** after a pull. Terminal nameplates are
read live from `meter_points.yaml` — edit there, no code change.

## Calibration sources (optional, free)

**EIA monthly LNG exports** — `eia-fetch --weeks 52` (needs `EIA_API_KEY`, register at
eia.gov/opendata). Loads the authoritative monthly U.S. LNG total to calibrate the
amber LNG terminals.

**AIS vessel tracking** — `ais-collect --duration 300` then `ais-infer --date today`
(needs `AISSTREAM_API_KEY`, register at aisstream.io). Detects whether an LNG carrier is
moored at a terminal. A carrier = a moored vessel with a large, beamy hull (length ≥250m
**and** beam ≥38m, or a gas-tanker type) — size is the reliable discriminator since AIS
type codes are sparse and 80-89 covers all tankers. The "60% of nameplate when a carrier
is at berth" estimate is a coarse *is-it-loading* proxy, not a measurement.

## Automation (Windows Task Scheduler)

| Task | What | Cadence |
|---|---|---|
| `NG-Feedgas-Daily-Pull` | all scrapers, yesterday/evening | daily 7:00 AM |
| `NG-Feedgas-Intraday-Fast` | 7 HTTP scrapers, today/auto | every 5 min |
| `NG-Feedgas-Intraday-Slow` | 2 Playwright scrapers, today/auto | every 15 min |
| `NG-Feedgas-AIS-Track` | AIS collect + infer | hourly |

Register the intraday tasks with `tools/register_intraday_tasks.ps1`. All run only while
logged on (no stored password). SQLite uses WAL + busy_timeout so concurrent task writes
don't collide.

## Tests

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests/ -q
```

Covers number parsing, cycle maps, upsert idempotency, `categorize`, and the TETCO
gas-day-selection regression guard.

## Architecture

```
src/ng_feedgas/
  cli.py            click CLI: pull / show / report / changes / eia-fetch / ais-*
  models.py         FlowRecord (tz-aware), Cycle/Direction literals
  validators.py     Part-6 reasonableness checks
  config/           load_config, categorize(), meter_points.yaml (terminal->meter map)
  scrapers/         base (with_retry) + 9 portal scrapers
  storage/          schema.sql, db (WAL, upsert, terminal_totals[_directional], terminal_series), export
  analysis/         stats (LNG-only), prompt (Part-5), changes (DoD/WoW/MoM)
  calibration/      eia (monthly LNG), ais (vessel tracking)
dashboard.py        Plotly Dash app
tools/              PowerShell task wrappers + discovery/probe scripts
tests/              pytest suite
```

## Known limitations

1. **~50% LNG coverage.** Calcasieu Pass + Golden Pass have no scraper; Cheniere CCPL,
   Cameron CIP, and private Sabine/Plaquemines feeders are off-EBB. Amber LNG totals are
   lower bounds — calibrate against EIA.
2. **Texas-intrastate Mexico crossings** (Trans-Pecos, Comanche Trail, NET Mexico, Valley
   Crossing) have no public daily OAC. CENAGAS monthly PDFs are the only free source
   (parser skeleton in `tools/cenagas_pdf_backfill.py`, ~30-day lag).
3. **Unit convention.** Portals publish Dth/d (≈ MMBtu/d); we treat Dth/d ≈ Mcf/d and
   divide by 1000 for MMcf/d.
4. **Scheduled quantities, not metered actuals.** EBBs post nominations, very close to
   physical flow on confirmed cycles but not custody-meter readings.

## License & scope

Research / internal use. Not affiliated with any pipeline operator. All data is public
FERC informational-posting data; respect each portal's terms of use.
