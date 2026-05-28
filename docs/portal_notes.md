# EBB Portal Notes — Final Coverage Map

Verified 2026-05-22 against live EBB postings.

## Quick stats

- **Pipelines functional:** 6 portals (KMI, Williams 1Line, Enbridge RTBA, Energy Transfer TGC, TC Energy via KMI, **TC eConnects via Playwright**)
- **Terminals captured:** 6 of 8 (Sabine Pass, Corpus Christi, Freeport, Cameron, Elba Island, Plaquemines)
- **Sample US total (2026-05-21 evening):** ~6,911 MMcf/d — roughly 55% of the true ~12,500 MMcf/d
- **Biggest remaining gaps:** Cove Point (no public EBB at all — dedicated Cove Point Pipeline is off-EBB), Calcasieu Pass (Venture Global — private), Creole Trail direct feed (only partial via Trunkline interconnect)

## Functional portals

### 1. Kinder Morgan — `pipeline2.kindermorgan.com`

Hosts informational postings for several pipelines under a common UI. Set `code=` query
parameter to select the pipeline; trigger the page's Excel download button to get all
rows for the selected gas day + cycle in one POST. Tested pipeline codes:

| Code | TSP | Pipeline | LNG terminals |
|---|---|---|---|
| NGPL | 6931794 | Natural Gas Pipeline Co. | Sabine Pass (Loc 46622), Corpus Christi (Loc 48934) |
| EEC  | 828834445 | Elba Express Co. | Elba Island (Loc 938000) |
| TGP  | 1939164 | Tennessee Gas Pipeline | Cameron (Loc 49446), Plaquemines (Loc 55833) |
| SNG  | 6900518 | Southern Natural Gas | (no high-volume LNG flows in tested data — bidirectional points often 0) |

Scraper: [src/ng_feedgas/scrapers/kmi.py](../src/ng_feedgas/scrapers/kmi.py)

### 2. Williams 1Line (Transco) — `1line.williams.com`

4-step flow: bootstrap → GET form → POST date+cycle+locationIDs → GET OACreport.jsp.
Cycle codes: 1=Timely, 2=Evening, 3=ID1, 4=ID2, 8=ID3, 5=Post, 7=Retro.

| Loc | Loc Name | Terminal | TSQ (sample) |
|---|---|---|---|
| 9009310 | LIGHTHOUSE ROAD M4662 MP 13 | Freeport LNG | 1,063 MMcf/d |

Cove Point delivery point not located in Transco's public OAC; the report shows mainline
segments and 223 named delivery locations but no clear Cove Point match.

Scraper: [src/ng_feedgas/scrapers/williams.py](../src/ng_feedgas/scrapers/williams.py)

### 3. Enbridge RTBA — `rtba.enbridge.com` (TETCO)

ASP.NET WebForms with `__doPostBack` Excel-CSV download. The cycle dropdown lists all
posted snapshots for the gas day under prefixes: TIMELY, LATE, LATEC, INTRDY, INTRDYC.

| Loc | Loc Name | Terminal | TSQ (sample) |
|---|---|---|---|
| 74568 | Kinder Morgan LNG Del (73568 Receipt) | Sabine Pass area | 386 MMcf/d |
| 79999 | FLNG - STRATTON RIDGE | Freeport LNG | 193 MMcf/d |
| 73882 | Sempra/Cameron - Gulf Market | Cameron LNG | 301 MMcf/d |
| 74530 | Gator Express-Plaquemines | Plaquemines (excluded: may double-count with TGP) | 1,497 MMcf/d |

Scraper: [src/ng_feedgas/scrapers/enbridge.py](../src/ng_feedgas/scrapers/enbridge.py)

### 4. Energy Transfer TGC — `tgcmessenger.energytransfer.com` (Trunkline)

Simplest portal: direct CSV at
`/ipost/capacity/operationally-available-by-location?asset=TGC&f=csv&extension=csv`.
Returns the most-recent posted snapshot (no date param). For historical days, use the
page form (not yet implemented).

| Loc | Loc Name | Terminal (indirect via Creole Trail) | TSQ (sample) |
|---|---|---|---|
| 82742 | BEAUREGARD PARISH INCT - CHENIERE CREOLE TRAIL | Sabine Pass | 265 MMcf/d |
| 80482 | LNG AT LAKE CHARLES - TGC | Lake Charles LNG (inactive) | 0 |

Scraper: [src/ng_feedgas/scrapers/energytransfer.py](../src/ng_feedgas/scrapers/energytransfer.py)

### 5. TC Energy TGP — via KMI portal

Delegates to KMI scraper with `code=TGP`. See section 1.

Scraper: [src/ng_feedgas/scrapers/tcenergy.py](../src/ng_feedgas/scrapers/tcenergy.py)

### 6. TC eConnects — Columbia Gas (TCO), Columbia Gulf (CGT), ANR

**Requires Playwright + Chromium** — the portal is fully JS-driven (changeAsset JS calls, SSRS ReportViewer iframe with no public URL parameters for CSV).

Flow:
1. Open `https://ebb.tceconnects.com/infopost/Default.aspx?assetid={N}`
2. `page.evaluate("changeAsset('{N}', '{label}')")` to set asset context
3. Navigate to `ReportViewer.aspx?/InfoPost/{report_path}&pAssetNbr={N}`
4. Wait 5 seconds for SSRS to render
5. `page.evaluate("$find('ReportViewer1').exportReport('CSV')")` triggers download
6. Capture with `page.expect_download()` → save → read with pandas

Supported assets:

| Code | Asset ID | Report Path | Pipeline |
|---|---|---|---|
| TCO | 51 | OperationallyAvailableCapacity | Columbia Gas Transmission |
| CGT | 14 | OperationallyAvailableCapacity | Columbia Gulf Transmission |
| ANR | 3005 | OperationallyAvailableCapacityANR | ANR Pipeline Company |

LNG-relevant hits found:

| Asset | Loc | Loc Name | Terminal | TSQ (sample) |
|---|---|---|---|---|
| CGT | 4246 | Cameron LNG | Cameron LNG | 0–372 MMcf/d (BD, varies) |
| CGT | 4267 | Wilkinson Bayou | Plaquemines | 640 MMcf/d |
| TCO | — | (no clear LNG match) | — | — |
| ANR | — | (no clear LNG match) | — | — |

Scraper: [src/ng_feedgas/scrapers/tceconnects.py](../src/ng_feedgas/scrapers/tceconnects.py)

**Caveat:** The SSRS ReportViewer URL does not take a date parameter — it shows the
most-recent posted snapshot. For historical-day data, the report has a date filter
control in the UI that we don't yet drive. Today's data is fine; backfills will pull
"latest" not "for-date".

## Remaining gaps and why

### Cove Point
**Status: Uncovered, definitively.** Cove Point LNG is fed by the Cove Point Pipeline,
a dedicated ~80-mile lateral operated by Cove Point LNG, LP. It interconnects with
Transco at Pleasant Valley (Loudoun County, VA) and with TCO/Columbia Gas. But the
Cove Point Pipeline itself does NOT post public OAC, and neither Transco nor TCO
shows a delivery point named "Cove Point" in their public reports. The 600+ MMcf/d of
Cove Point feedgas flows through the off-EBB lateral and is invisible to public scraping.

**Path forward:** EIA weekly LNG export report is the only public daily-ish proxy. No
real-time scraping solution exists with public data.

### Calcasieu Pass LNG
**Status: Uncovered.** Served by Venture Global's TransCameron Pipeline, which is a
shipper-private system with no public EBB. TGP's "Lake Charles LNG" point (Loc 80482 on
Trunkline) is a separate project, not Calcasieu Pass.

**Path forward:** Venture Global publishes monthly export reports; EIA covers this
terminal in the weekly LNG report. No daily feedgas-quality public source identified.

### Creole Trail direct feed
**Status: Partial.** Creole Trail Pipeline (Cheniere-owned) has no standalone public EBB.
The Trunkline interconnect (Loc 82742, ~265 MMcf/d) captures one slice; other Creole
Trail receipts (from NGPL, TETCO laterals, others) are NOT captured. Sabine Pass total
is currently 1,474 MMcf/d vs typical ~3,200 — gap of ~1,700 MMcf/d unaccounted.

### Gulf South Pipeline (Boardwalk) — Corpus Christi secondary
**Status: Deferred.** Aggregator `piperiv.com/ip/gulf-south` exists but API access is
premium. Direct portal `ebbs.gulfsouthpl.com` not yet probed in depth.

### ANR Pipeline — Cameron secondary
**Status: Blocked.** `ebb.anrpl.com` returns HTTP 503 to my requests (likely Cloudflare
bot block). Also accessible via TC eConnects (asset 3005) but same JS issue as CGT.

### CIP (Cameron Interstate Pipeline), SNG via Sempra, Bridgeline, Columbia Gulf
**Status: Various.** CIP and Bridgeline are shipper-only customer portals. SNG via
Southern Co Gas portal requires investigation but SNG via KMI works (Cameron flow not
visible on the SNG report today). Columbia Gulf is on TC eConnects (asset 14) — same
JS limitation.

## Per-terminal sample (2026-05-21 evening cycle)

```
Sabine Pass      1,474 MMcf/d  (NGPL 823 + TETCO/KMI-LNG 386 + Trunkline/Creole 265)
Corpus Christi     586 MMcf/d  (NGPL only)
Freeport LNG     1,256 MMcf/d  (Transco 1,064 + TETCO/Stratton Ridge 193)
Cameron LNG        998 MMcf/d  (TGP 696 + TETCO/Sempra 301)
Cove Point           0 MMcf/d  (CGT portal not scrapable)
Elba Island        131 MMcf/d  (EEC; SNG also configured but currently 0)
Calcasieu Pass       0 MMcf/d  (Venture Global private)
Plaquemines      1,826 MMcf/d  (TGP only; TETCO→Gator excluded to avoid double-count)
TOTAL            6,270 MMcf/d  (~50% of true US feedgas)
```
