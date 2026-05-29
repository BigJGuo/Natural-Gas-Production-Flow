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

### 7. TC Energy "Ganesha" EBB — `tcplus.com`  (NEW, working)

Verified 2026-05-29. Plain HTTP, no JS needed (FAST scraper). Hosts exactly four
TC Energy pipelines: **GTN, Great Lakes, North Baja, Tuscarora** (Viking and
Northern Border are NOT here — see below).

The OAC "CSV" button POSTs to `https://www.tcplus.com/{PIPELINE}/Export/Generate`
(pipeline path may contain a space, e.g. `Great Lakes`) with form fields:

```
serviceTypeName = Ganesha.InfoPost.Service.OperationalCapacity.OperationalCapacityService, Ganesha.InfoPost.Service
filterTypeName  = Ganesha.InfoPost.ViewModels.GasDayAndCycleTypeFilterViewModel, Ganesha.InfoPost
templateType    = 6
exportType      = 1                  # 1=CSV 2=Excel 3=PDF 4=Txt 5=Tab
filter.GasDay   = MM/DD/YY
filter.CycleType= 1 Timely | 4 Evening | 2 Intraday1 | 3 Intraday2 | 5 Intraday3
customExtension =
```

CSV = 3 metadata rows + 1 blank, then header at row index 4:
`Loc Name, Loc, Loc Purp Desc, Loc/QTI, Flow Ind, DC, OPC, TSQ, OAC, IT, All Qty Avail`.
TSQ is MMBtu/d → /1000 = MMcf/d. Honors `--date` and `--cycle`.

| Pipeline | Loc | Loc Name | Crossing | Flow | TSQ (2026-05-29 eve) |
|---|---|---|---|---|---|
| GTN | 3498 | KINGSGATE | Kingsgate, ID (Canada import) | R | **2,008.7 MMcf/d** |
| Great Lakes | 33975 | EMERSON RECEIPT | Emerson, MN (Canada import) | R | **1,447.6 MMcf/d** |
| Great Lakes | 11772 | ST CLAIR DELIVERY | St. Clair / Sarnia ON (export) | D | **644.7 MMcf/d** |
| North Baja | 336408 | OGILBY DEL | Ogilby, CA (Mexico export) | D | 420.6 — *NOT loaded: same gas as the existing EPNG North Baja point; would double-count* |

Scraper: [src/ng_feedgas/scrapers/tcplus.py](../src/ng_feedgas/scrapers/tcplus.py).
Net effect: ~+4.1 Bcf/d of previously-missing Canada flow now captured daily.

### 8. DT Midstream Trellis PTM — `dtmidstream.trellisenergy.com`  (NEW, working)

Viking Gas Transmission (VGT, tspId=9) moved to DT Midstream's Trellis PTMS
Nov-2025. The OAC report is PUBLIC (no login) via two JSON endpoints (verified
2026-05-29):

1. List postings (jqGrid; needs `Referer` = the viewInfoPostingReportTable page):
   `/ptms/public/infopost/getInfoPostRpts.do?tspId=9&rptId=2&downloadInd=0&searchInd=0&showLatestInd=0&_search=false&nd=1&rows=200&page=1&sidx=&sord=asc&_=1`
   → `{"rows":[{"id":67505000000,"formattedGasDay":"05/29/2026","cycleCode":"Evening",...}]}`.
2. Data file: `/ptms/public/infopost/getInfoPostRptTxtFile.do?infoPostDataId={id}&level=1`
   → JSON with `columnNames` + an `xmlData` `<row><cell>…` string. Columns:
   Loc Name, Loc, Loc Prop, Loc Purp Desc, Flow Ind, Loc/QTI, All Qty Avail, DC,
   OPC, TSQ, OAC, IT, Qty Reason. TSQ MMBtu/d → /1000 MMcf/d.

| Loc | Loc Name | Crossing | Flow | TSQ (2026-05-29 eve) |
|---|---|---|---|---|
| 33973 | Emerson | Emerson, MN (Canada import) | R | **400.2 MMcf/d** |

Scraper: [src/ng_feedgas/scrapers/trellis.py](../src/ng_feedgas/scrapers/trellis.py)
(FAST/HTTP). Combined with Great Lakes, the Emerson terminal is now ~1.85 Bcf/d.

### 9. National Fuel PeopleSoft — Empire Pipeline  (NEW, working, Playwright)

Empire's OAC is a PUBLIC (no-login) PeopleSoft component:
`https://sbsprd2.natfuel.com/psc/sbsprd/NFSBS/SBSPRD/c/NFOM_INFORMATIONAL_POSTINGS.NFOC_OPER_AVAIL_1.GBL`.
The page exposes CSV-download links `NF_FILE_ATT_WRK_NF_CSV_DWN_BTN$0/$1/$2`
($0=Evening, $1=Timely, $2=Prelim). Plain `requests` ICAction POSTs just re-render
the page; the file only comes via the browser download, so we drive it with
**Playwright** + `expect_download()`. CSV has ~20 metadata lines then a header
(Loc Name, Loc, …, Total Scheduled Quantity, Flow Indicator).

| Loc | Loc Name | Crossing | Flow | TSQ (2026-05-29) |
|---|---|---|---|---|
| 421079 | TCPL - Niagara* | Niagara/Chippawa (Empire↔TC) | D | **315.3 MMcf/d** (US→Canada; receipt side 0) |

Scraper: [src/ng_feedgas/scrapers/empire.py](../src/ng_feedgas/scrapers/empire.py)
(SLOW/Playwright). Distinct pipe from the TGP Niagara delivery captured via KMI.

### Genuinely blocked (no anonymous public source found)

- **Roadrunner Gas Transmission** (Waha→San Elizario→Mexico) and **Northern Border**
  (Port of Morgan, MT, ~2 Bcf/d) are both **ONEOK**-affiliated. ONEOK routes all
  pipeline data — including FERC postings — through the **myQuorum Customer Portal**
  (`qptmintra.oneok.com` → SecureAuth login), which requires free registration. No
  anonymous public OAC posting was found (oneok.com/rgt has no infopost link;
  northernborder.com does not resolve). **Blocked without an account** — not built.

### CENAGAS intrastate-Mexico volumes — corrected finding (2026-05-29)

The free CENAGAS monthly "Volumen" PDF is **extraction-only** (domestic E/N nodes,
e.g. E016=AEROPUERTO). It does **not** contain the US-border IMPORT (injection)
volumes. Those are the "V" series (Origen del Gas = Importación) in CENAGAS's node
catalog (`/GestionComercial/Nodos`); the correct crosswalk is:
`Mexico - NET Mexico = V061 RAMONES` (Net Mexico Pipeline, Camargo),
`Mexico - Valley Crossing = V074 MONTEGRANDE` (Sur de Texas-Tuxpan marine),
plus V033 Tennessee / V032,V034 KM Border / V037 KM Texas / V067 Houston Pipeline-ET.
But these V-node daily volumes are **not** in the free PDF and no other free
downloadable source was found, so intrastate-Mexico export flows remain unloadable.
Run `python tools/cenagas_pdf_backfill.py --catalog` to reproduce the crosswalk.

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
