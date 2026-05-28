# LNG Feedgas Data Model — Reference & Daily Prompt Template

> **Purpose:** Accurately aggregate U.S. LNG feedgas flows using free pipeline EBB data.
> **Unit:** MMcf/d unless otherwise noted.
> **Accuracy Note:** Always use *Confirmed/Scheduled* quantities, not just nominations. Cross-validate against EIA monthly data when available.

---

## PART 1 — Terminal-to-Pipeline Master Map

| Terminal | State | Operator | Primary Pipeline(s) | Pipeline Operator | Key EBB Meter Search Terms |
|---|---|---|---|---|---|
| **Sabine Pass LNG** | LA | Cheniere | Creole Trail Pipeline | Kinder Morgan | "Sabine Pass Liquefaction", "SPL" |
| **Sabine Pass LNG** | LA | Cheniere | NGPL | Kinder Morgan | "Sabine Pass", "SPL" |
| **Sabine Pass LNG** | LA | Cheniere | Texas Eastern (TETCO) | Enbridge | "Sabine Pass Liquefaction" |
| **Sabine Pass LNG** | LA | Cheniere | Trunkline | Energy Transfer | "Sabine Pass" |
| **Corpus Christi LNG** | TX | Cheniere | NGPL | Kinder Morgan | "Corpus Christi Liquefaction", "CCL" |
| **Corpus Christi LNG** | TX | Cheniere | Gulf South Pipeline | Boardwalk | "Corpus Christi", "CCL" |
| **Freeport LNG** | TX | Freeport LNG Dev. | Texas Eastern (TETCO) | Enbridge | "Freeport LNG" |
| **Freeport LNG** | TX | Freeport LNG Dev. | Transcontinental (Transco) | Williams | "Freeport LNG" |
| **Cameron LNG** | LA | Sempra Infrastructure | Cameron Interstate Pipeline (CIP) | Southern Co. Gas | "Cameron LNG", "CIP Delivery" |
| **Cameron LNG** | LA | Sempra Infrastructure | Southern Natural Gas (SNG) | Southern Co. Gas | "Cameron LNG" |
| **Cameron LNG** | LA | Sempra Infrastructure | ANR Pipeline | TC Energy | "Cameron LNG" |
| **Cove Point LNG** | MD | Berkshire Hathaway Energy | Transcontinental (Transco) | Williams | "Cove Point", "Station 165" |
| **Cove Point LNG** | MD | Berkshire Hathaway Energy | Columbia Gas Transmission (CGT) | Various | "Cove Point", "Dominion Cove Point" |
| **Elba Island LNG** | GA | Shell / Kinder Morgan | Elba Express Pipeline | Kinder Morgan | "Elba Island", "Elba Liquefaction" |
| **Elba Island LNG** | GA | Shell / Kinder Morgan | Southern Natural Gas (SNG) | Southern Co. Gas | "Elba Island", "Shell LNG" |
| **Calcasieu Pass LNG** | LA | Venture Global | Tennessee Gas Pipeline (TGP) | TC Energy | "Calcasieu Pass", "Venture Global" |
| **Calcasieu Pass LNG** | LA | Venture Global | Bridgeline Holdings | Bridgeline | "Calcasieu Pass" |
| **Plaquemines LNG** | LA | Venture Global | Tennessee Gas Pipeline (TGP) | TC Energy | "Plaquemines", "Venture Global Plaquemines" |
| **Plaquemines LNG** | LA | Venture Global | Columbia Gulf Transmission | Various | "Plaquemines LNG" |

---

## PART 2 — EBB Access Directory

Verify these URLs are current before each use — EBB portals occasionally move.

| Pipeline | Operator | EBB URL | Notes |
|---|---|---|---|
| Creole Trail | Kinder Morgan | https://pipeline2.kindermorgan.com | Also covers NGPL, Elba Express |
| NGPL | Kinder Morgan | https://pipeline2.kindermorgan.com | Same KMI portal |
| Elba Express | Kinder Morgan | https://pipeline2.kindermorgan.com | Same KMI portal |
| Texas Eastern (TETCO) | Enbridge | https://www.texaseastern.com | Enbridge Gas Transmission portal |
| Transcontinental (Transco) | Williams | https://www.1line.williams.com | Williams 1Line portal |
| Tennessee Gas Pipeline (TGP) | TC Energy | https://tgp.eprod.com | TC Energy EBB |
| ANR Pipeline | TC Energy | https://www.tcenergy.com/operations/gas/ebbs | TC Energy EBB |
| Cameron Interstate (CIP) | Southern Co. Gas | Contact/portal via Sempra | Dedicated pipe, limited public EBB |
| Southern Natural Gas (SNG) | Southern Co. Gas | https://www.southernunionco.com | SNG EBB portal |
| Trunkline | Energy Transfer | https://www.energytransfer.com/gs_ebbs.aspx | Energy Transfer EBB |
| Gulf South Pipeline | Boardwalk Pipelines | https://ebbs.gulfsouthpl.com | Boardwalk portal |
| Columbia Gulf | Various | Verify current operator | Ownership has shifted — verify |

---

## PART 3 — Daily Data Pull Checklist

**Recommended cycles to pull:**
- **Timely Cycle** (~10:00 AM CPT, day-ahead) — first look at next-day feedgas plan
- **Evening Cycle** (~9:00 PM CPT, day-ahead) — refined next-day view, most used for forecasting
- **Confirmed/Final** — most accurate for actuals; use for historical model

### Step-by-Step Workflow

**[ ] Step 1 — Log into each EBB portal**
Navigate to each EBB in the directory above. Most are public; some require free registration.

**[ ] Step 2 — Filter by location/meter point**
Use the search terms in Part 1 to isolate LNG-related meter points. Look under "Scheduled Quantities," "Confirmations," or "Flowing Gas" depending on the portal's terminology.

**[ ] Step 3 — Record flows by terminal and pipeline**

For each terminal, record:
- Date (gas day)
- Nomination cycle used
- Pipeline name
- Meter point / location ID
- Scheduled quantity (MMcf/d)
- Flow direction (confirm it is INTO the terminal, not send-out)

**[ ] Step 4 — Sum by terminal**
Add all pipeline meter quantities feeding into each terminal. This is the terminal-level feedgas number.

**[ ] Step 5 — Sum all terminals**
This is total U.S. LNG feedgas for the day (MMcf/d).

---

## PART 4 — Data Entry Table (Copy Daily)

```
Gas Day: ____________     Cycle: [ ] Timely  [ ] Evening  [ ] Confirmed

TERMINAL                  | PIPELINE           | METER POINT          | MMcf/d
--------------------------|--------------------|-----------------------|-------
Sabine Pass               | Creole Trail       |                       |
Sabine Pass               | NGPL               |                       |
Sabine Pass               | TETCO              |                       |
Sabine Pass               | Trunkline          |                       |
                          | SABINE PASS TOTAL  |                       | ______
--------------------------|--------------------|-----------------------|-------
Corpus Christi            | NGPL               |                       |
Corpus Christi            | Gulf South         |                       |
                          | CORPUS CHRISTI TOT.|                       | ______
--------------------------|--------------------|-----------------------|-------
Freeport LNG              | TETCO              |                       |
Freeport LNG              | Transco            |                       |
                          | FREEPORT TOTAL     |                       | ______
--------------------------|--------------------|-----------------------|-------
Cameron LNG               | CIP                |                       |
Cameron LNG               | SNG                |                       |
Cameron LNG               | ANR                |                       |
                          | CAMERON TOTAL      |                       | ______
--------------------------|--------------------|-----------------------|-------
Cove Point                | Transco            |                       |
Cove Point                | CGT                |                       |
                          | COVE POINT TOTAL   |                       | ______
--------------------------|--------------------|-----------------------|-------
Elba Island               | Elba Express       |                       |
Elba Island               | SNG                |                       |
                          | ELBA ISLAND TOTAL  |                       | ______
--------------------------|--------------------|-----------------------|-------
Calcasieu Pass            | TGP                |                       |
Calcasieu Pass            | Bridgeline         |                       |
                          | CALCASIEU TOTAL    |                       | ______
--------------------------|--------------------|-----------------------|-------
Plaquemines               | TGP                |                       |
Plaquemines               | Columbia Gulf      |                       |
                          | PLAQUEMINES TOTAL  |                       | ______
========================= |====================|=======================|=======
                          | U.S. TOTAL FEEDGAS |                       | ______
```

---

## PART 5 — Daily Analysis Prompt (Paste into Claude)

Use this prompt after filling out the data entry table above. Replace bracketed fields with your actual data.

---

```
You are a natural gas market analyst. I am providing you with today's U.S. LNG feedgas 
data pulled from pipeline EBBs. Please analyze this data accurately.

Gas Day: [DATE]
Nomination Cycle: [TIMELY / EVENING / CONFIRMED]

FEEDGAS DATA (MMcf/d):
Sabine Pass:       [X] MMcf/d
Corpus Christi:    [X] MMcf/d
Freeport LNG:      [X] MMcf/d
Cameron LNG:       [X] MMcf/d
Cove Point:        [X] MMcf/d
Elba Island:       [X] MMcf/d
Calcasieu Pass:    [X] MMcf/d
Plaquemines:       [X] MMcf/d
TOTAL U.S.:        [X] MMcf/d

Prior gas day total: [X] MMcf/d
7-day average:       [X] MMcf/d
30-day average:      [X] MMcf/d

Known outages / maintenance today: [LIST ANY, OR "NONE KNOWN"]

Please provide:
1. Day-over-day change and which terminals drove it
2. Any terminal running notably below its nameplate capacity (flag if >20% below)
3. Week-over-week trend summary
4. Any anomalies or data points worth flagging
5. A one-line total feedgas summary suitable for a morning report
```

---

## PART 6 — Validation Checklist

Run these checks before finalizing any feedgas number:

**Reasonableness bounds (approximate nameplate capacities):**
| Terminal | Approx. Max Capacity (MMcf/d) |
|---|---|
| Sabine Pass (Trains 1-6) | ~3,500 |
| Corpus Christi (Trains 1-3) | ~2,100 |
| Freeport LNG (Trains 1-3) | ~2,100 |
| Cameron LNG (Trains 1-3) | ~1,700 |
| Cove Point (Train 1) | ~750 |
| Elba Island (10 units) | ~350 |
| Calcasieu Pass | ~1,000 |
| Plaquemines | ~1,400 |
| **U.S. Total** | **~12,900** |

> If any terminal is running >10% above its nameplate, recheck your meter point — you may be double-counting a meter or capturing send-out flows instead of feedgas.

**Other sanity checks:**
- [ ] Total U.S. feedgas should be between ~8,000–13,000 MMcf/d under normal operating conditions
- [ ] Confirm flow direction on each meter is *receipts into terminal*, not deliveries out
- [ ] Cross-check against EIA weekly LNG data (released Thursdays) when available
- [ ] Check vessel AIS (MarineTraffic.com, free tier) — terminals loading ships should show high feedgas; idle terminals should show low or zero
- [ ] If a terminal shows sudden zero flow, check for FERC force majeure notices or operator announcements

---

## PART 7 — Reference Benchmarks

Use these for model calibration:

| Source | Frequency | Lag | URL |
|---|---|---|---|
| EIA Natural Gas Weekly | Weekly | ~1 week | https://www.eia.gov/naturalgas/weekly |
| EIA LNG Monthly | Monthly | ~6-8 weeks | https://www.eia.gov/naturalgas/lng |
| FERC Tariff Filings | As-filed | Real-time | https://www.ferc.gov |
| MarineTraffic (AIS) | Real-time | Minutes | https://www.marinetraffic.com |

---

*Template last updated: May 2026. Verify EBB URLs, terminal ownership, and capacity figures periodically as they change.*
