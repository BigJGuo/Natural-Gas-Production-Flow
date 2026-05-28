# ng_feedgas — U.S. LNG Feedgas EBB Scraper

Automates the daily workflow defined in [NG Production/LNG_Feedgas_Template.md](NG%20Production/LNG_Feedgas_Template.md):
pulls scheduled-quantity data from pipeline EBB (Electronic Bulletin Board) portals, sums per LNG
terminal, stores history in SQLite, and emits the filled-in Part-4 table and Part-5 analysis prompt.

> **Status:** Proof of concept, functional. Five of eight terminals are scraped end-to-end
> against the live Kinder Morgan portal. Williams Transco scraper is a stub. See
> `docs/portal_notes.md` for the verified meter-point IDs and the remaining gaps.

## Coverage

6 scrapers covering 6 of 8 terminals. Sample US total: **6,911 MMcf/d for 2026-05-21 evening** (~55% of typical 12,500 MMcf/d US total).

| Terminal | Pipelines captured | Sample MMcf/d | Status |
|---|---|---|---|
| Sabine Pass | NGPL + TETCO + Trunkline-Creole | 1,474 | Partial (Creole Trail direct still missing) |
| Corpus Christi | NGPL | 586 | Partial (Gulf South missing) |
| Freeport LNG | Transco + TETCO | 1,256 | Good |
| Cameron LNG | TGP + TETCO + Col Gulf | 998 | Good |
| Cove Point | none | 0 | **Blocked** — Cove Point Pipeline is dedicated short line, off-EBB |
| Elba Island | EEC | 131 | Good |
| Calcasieu Pass | none | 0 | **Blocked** — Venture Global TransCameron is private |
| Plaquemines | TGP + Col Gulf | 2,466 | Good (over Phase-1 nameplate; reflects Phase-2 ramp) |

See [docs/portal_notes.md](docs/portal_notes.md) for the full coverage map, verified meter IDs, and the rationale for each gap.

## Scrapers

| Scraper | Portal | Pipelines | Tech |
|---|---|---|---|
| `kmi` | pipeline2.kindermorgan.com | NGPL, EEC, TGP, SNG (via `code=` param) | requests + viewstate POST + xlsx |
| `williams` | 1line.williams.com | Transco | requests + 4-step JSP flow |
| `tcenergy` | (delegates to KMI with code=TGP) | TGP | — |
| `enbridge` | rtba.enbridge.com | TETCO | requests + viewstate POST + CSV |
| `et_tgc` | tgcmessenger.energytransfer.com | Trunkline | requests + direct CSV URL |
| `tceconnects` | ebb.tceconnects.com | TCO, Columbia Gulf, ANR | **Playwright** (SSRS, JS-only) |

> The `tceconnects` scraper requires Playwright + a Chromium install:
> `pip install playwright && python -m playwright install chromium`

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Calibration sources (EIA + AIS) — optional

Two external sources fill gaps the pipeline EBBs miss. Both are free but require one-time API key setup.

### EIA weekly LNG export totals (calibration baseline)

1. Register for a free API key at https://www.eia.gov/opendata/register.php (instant).
2. Set the env var: `setx EIA_API_KEY "your-key"` (open a new PowerShell after).
3. Fetch:
   ```powershell
   $env:PYTHONPATH = "src"
   python -m ng_feedgas eia-fetch --weeks 52
   ```
4. The dashboard now overlays EIA's weekly LNG export total (red dashed line) on the U.S. Total chart — compare against your scraped total to see how big your scraping gap is each week.

### AIS vessel tracking (Cove Point + Calcasieu Pass proxy)

1. Register for a free API key at https://aisstream.io (instant).
2. Set the env var: `setx AISSTREAM_API_KEY "your-key"` (open a new PowerShell after).
3. Collect vessel observations (run this on a schedule — every hour works well):
   ```powershell
   python -m ng_feedgas ais-collect --duration 300   # listen 5 min
   ```
   Each run opens a WebSocket to aisstream.io, listens for AIS broadcasts in the bounding boxes around each LNG terminal, and writes observations to the SQLite DB.
4. Roll up daily:
   ```powershell
   python -m ng_feedgas ais-infer --date today
   ```
   The inference rule is crude: if any LNG carrier was moored (slow or status=5) at a terminal's berth on that day, estimate feedgas at 60% of nameplate; otherwise 0.
5. Dashboard now shows an "AIS-inferred" bar on the Terminal Snapshot chart (orange, alongside scraped blue), plus an "AIS Vessel Tracking" table at the bottom listing ships-at-berth per terminal for the selected day.

This is **directional** data — useful to detect Cove Point or Calcasieu Pass activity (or lack thereof), not for precise volume. Cross-check against the EIA weekly total to calibrate the 60% loading-rate assumption.

## How it works

Each scraper drives one portal's "download the daily snapshot" flow:

- **KMI portal**: GET form → harvest viewstate → POST btnDownload → read .xlsx (`pandas.read_excel(header=3)`)
- **Williams 1Line**: bootstrap → GET form → POST date+cycle+locationIDs → GET OACreport.jsp → regex-parse HTML rows
- **Enbridge RTBA**: GET form → harvest viewstate + pick latest LATEC/INTRDYC option → POST `__doPostBack` for download link → read CSV
- **Energy Transfer TGC**: GET landing → GET `?f=csv&extension=csv` → read CSV

All scrapers extract the same canonical `FlowRecord` (gas_day, cycle, terminal, pipeline, meter_point, mmcfd, direction, source_url) and upsert into SQLite. The same downstream code (CSV export, Part-5 prompt, validators) works for all of them.

## Usage

```powershell
# Pull yesterday's evening cycle from all scrapers (KMI + TGP-via-KMI run; Williams skips)
$env:PYTHONPATH = "src"
python -m ng_feedgas pull --date yesterday --cycle evening --pipeline all

# Pull only one scraper
python -m ng_feedgas pull --date today --cycle evening --pipeline kmi

# Print the Part-4 entry table to stdout
python -m ng_feedgas show --date yesterday --cycle evening

# Write the Part-4 CSV and Part-5 analysis prompt under data/exports/
python -m ng_feedgas report --date yesterday --cycle evening
```

Use the **evening** cycle for forecasting (posted ~9 PM CPT, refined view of next gas day).
Use **confirmed** (which maps to KMI's "BEST AVAILABLE") for historical backfills.

The Part-5 prompt is a plain text file ready to paste into Claude.
"Known outages / maintenance today" is left as `[FILL IN MANUALLY OR "NONE KNOWN"]` —
the scraper has no automated source for that.

## Outputs

```
data/
  feedgas.db                          SQLite history. One row per gas-day/cycle/meter point.
  exports/
    2026-05-22_evening.csv            Part-4 entry table as CSV.
    2026-05-22_evening_prompt.txt     Part-5 analysis prompt, ready to paste into Claude.
```

## Validators (Part 6)

After each `pull`, the CLI runs the Part-6 reasonableness checks and prints warnings to stderr:

- Any terminal whose total exceeds 110% of nameplate (likely double-count or wrong direction).
- Any active terminal below 20% of nameplate (possible outage or scraper miss).
- U.S. total outside 8,000–13,000 MMcf/d (likely missing a scraper).

Warnings don't block writes; they print to stderr so cron logs surface them.

## Architecture

```
src/ng_feedgas/
  cli.py                  click CLI: pull / report / show
  models.py               FlowRecord dataclass + Cycle/Direction literals
  validators.py           Part-6 reasonableness checks
  config/
    __init__.py           Config loader
    meter_points.yaml     Terminal -> pipeline -> meter map
  scrapers/
    base.py               BaseScraper, ScrapeContext
    kmi.py                Functional: KMI OpAvailPoint by code/day/cycle
    williams.py           Skeleton
    tcenergy.py           Skeleton
  storage/
    schema.sql            CREATE TABLE flows
    db.py                 connect, upsert_flows, terminal_totals, us_totals_range
    export.py             Part-4 CSV + plaintext renderers
  analysis/
    stats.py              DailyStats: DoD, 7d, 30d
    prompt.py             Part-5 prompt filler
docs/
  portal_notes.md         Phase 0 findings + action items
data/
  feedgas.db              SQLite (created on first run)
  exports/                Daily CSVs and prompt files
```

## Known limitations

1. **Missing pipelines.** No coverage for Williams Transco (Freeport + Cove Point), Creole
   Trail (the second Sabine Pass feed), Gulf South (Corpus Christi secondary), TETCO,
   Trunkline, ANR, SNG, CIP, Columbia Gulf, Columbia Gas. POC totals will run ~4,000 MMcf/d
   short of the true US total of ~12,000 MMcf/d.
2. **Calcasieu Pass.** Not yet matched to any TGP meter in the downloaded grid. Likely
   under a Venture Global TransCameron name we haven't identified.
3. **Plaquemines > nameplate.** Plaquemines often runs above its 1,400 MMcf/d nominal
   nameplate after Phase 2 commissioning. The validator flags this; bump the YAML value
   when comfortable.
4. **Unit conversion.** KMI publishes in Dth/d (1 Dth = 1 MMBtu). Industry convention treats
   Dth/d ≈ Mcf/d for feedgas (heat content ~1 Btu/scf), so we divide by 1000 to get MMcf/d.
   For tighter accuracy, override per-pipeline using each EBB's "Meas Basis Desc" column.
5. **Rate limiting.** Default 2.5 s delay between requests (`--delay`). KMI portal was
   stable during dev testing.
6. **HTML / portal drift.** If KMI changes the page layout or the Excel download format,
   the scraper raises `ParseError` loudly rather than emitting zeros silently.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `KMI: no row matched terminal=...` | Loc ID in YAML doesn't appear in downloaded Excel for that cycle | Re-download manually and check the `Loc` column for the right meter; update `meter_points.yaml` |
| `KMI: btnDownload not found on form page` | KMI re-skinned the form | Re-inspect the form page and find the new button name |
| `KMI: missing required columns in xlsx` | KMI changed the Excel column layout | Re-inspect a downloaded `.xlsx` and update `REQUIRED_COLUMNS` / `EXCEL_HEADER_ROW` in `kmi.py` |
| `KMI: unexpected Content-Type` | Download POST returned an error page instead of Excel | The viewstate may have changed; check the form HTML for the new button name |
| Validation warns "U.S. total outside range" | Williams scraper not running yet | Expected for POC; add 6,000+ MMcf/d once Williams + Creole Trail go live |
| HTTP 403 / 429 | Rate limit | `--delay 10` or run less often |

## License & scope

Research / internal use. Not affiliated with any pipeline operator. All data is public
FERC informational posting data; respect each portal's terms of use.
