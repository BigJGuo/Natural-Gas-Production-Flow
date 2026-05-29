"""CENAGAS monthly PDF parser + node-catalog crosswalk (Mexico).

CENAGAS publishes monthly per-node daily-volume PDFs at:
  https://boletin-gestor.cenagas.gob.mx/Docs/Volumen/Volumen_{month}_{year}.pdf

KEY FINDING (2026-05-29): this PDF is EXTRACTION-only. It lists prefixes E and N
(domestic Mexican offtakes — e.g. E016 = "AEROPUERTO"). It does NOT contain the
US→Mexico IMPORT volumes. Those are the separate "V" (importación) injection
nodes, which are not published in this free PDF. So this PDF, despite the prior
assumption, is NOT a source of intrastate-Mexico export flows (NET Mexico,
Valley Crossing, Trans-Pecos, Comanche Trail). See IMPORT_NODE_CROSSWALK for the
correct US-border node identities (from CENAGAS's /GestionComercial/Nodos catalog).

Caveats:
  - PDFs are released ~30 days after month-end (not daily real-time).
  - Values are in MMpcd (≈ MMcf/d).

Run:
  python tools/cenagas_pdf_backfill.py --month enero --year 2026     # parse the PDF (extraction nodes)
  python tools/cenagas_pdf_backfill.py --catalog                     # print the live import-node crosswalk
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen
import ssl

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "cenagas"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


# ---------------------------------------------------------------------------
# IMPORTANT FINDING (verified 2026-05-29 against CENAGAS's own node catalog at
# /GestionComercial/Nodos):
#
# The monthly "Volumen Conducido" PDF (Volumen_{month}_{year}.pdf) contains ONLY
# EXTRACTION nodes — prefixes E (106) and N (69), which are *domestic* Mexican
# offtakes (e.g. E016 = "AEROPUERTO", E017 = "ALTAMIRA"). It does NOT contain the
# US-border IMPORT volumes. So the prior guesses (E016 → NET Mexico, etc.) were
# WRONG, and US→Mexico export volumes CANNOT be read from this PDF.
#
# The US-border imports are the SEPARATE "V" (importación) injection nodes, which
# are NOT published in this free monthly PDF (their daily volumes live only in the
# interactive Inyecciones/Extracciones dashboard, not in a downloadable report we
# have cracked). IMPORT_NODE_CROSSWALK below records the correct V-node → US-pipe
# mapping (straight from the catalog's interconnect descriptions) so that, once an
# injection-volume source is available, the crosswalk is ready. Until then there is
# NO free daily/monthly source of these volumes that we can load.
#
# Run `python tools/cenagas_pdf_backfill.py --catalog` to re-fetch and print the
# live import-node catalog.
# ---------------------------------------------------------------------------

# Correct US-border IMPORT nodes (CENAGAS "V" series, Origen del Gas = Importación).
# Source: catalog interconnect descriptions. `terminal` ties to meter_points.yaml.
# NOTE: volumes for these are NOT in the monthly extraction PDF (see above).
IMPORT_NODE_CROSSWALK = {
    "V061": {"name": "RAMONES",      "desc": "Net Mexico Pipeline (Frontera EE.UU.-Camargo)",        "terminal": "Mexico - NET Mexico"},
    "V074": {"name": "MONTEGRANDE",  "desc": "Sistema Marino Sur de Texas - Tuxpan (Valley Crossing)","terminal": "Mexico - Valley Crossing"},
    "V033": {"name": "IMPTENNESSEE", "desc": "Tennessee Gas Pipeline (Frontera EE.UU.-Reynosa)",      "terminal": None},
    "V032": {"name": "IMPCORAL",     "desc": "KM Border Pipeline (Frontera EE.UU.-Argüelles)",        "terminal": None},
    "V034": {"name": "IMPTETCO",     "desc": "KM Border Pipeline (Frontera EE.UU.-Reynosa)",          "terminal": None},
    "V037": {"name": "KMMTYINY",     "desc": "KM Texas Pipeline (Frontera EE.UU.-Mier)",              "terminal": None},
    "V067": {"name": "IMPENERGT",    "desc": "Houston Pipeline / Energy Transfer (Frontera-Argüelles)","terminal": None},
    "V030": {"name": "GLORIADIOS",   "desc": "IEnova Pipelines (CS Gloria a Dios)",                   "terminal": None},
}

# Loadable map for the EXTRACTION PDF. Empty because no extraction (E/N) node is a
# US-border import — loading from this PDF would only ever be domestic offtakes,
# which are out of scope. Kept (empty) so load_to_db() is a safe no-op. Populate
# only if a node is genuinely a verified US-border point present in this PDF.
CENAGAS_POINT_MAP: dict = {}


def download_pdf(month: str, year: int) -> Path:
    if month.lower() not in MONTHS_ES:
        raise SystemExit(f"month must be one of {MONTHS_ES}")
    url = (
        f"https://boletin-gestor.cenagas.gob.mx/Docs/Volumen/"
        f"Volumen_{month.lower()}_{year}.pdf"
    )
    out = DATA_DIR / f"Volumen_{month.lower()}_{year}.pdf"
    if out.exists() and out.stat().st_size > 10000:
        print(f"  (cached) {out}")
        return out
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 Chrome/120"})
    print(f"  downloading {url}")
    with urlopen(req, context=ctx, timeout=60) as r:
        out.write_bytes(r.read())
    if out.stat().st_size < 10000:
        out.unlink()
        raise SystemExit(f"PDF for {month}/{year} not yet posted by CENAGAS")
    return out


def _days_in_month(month: str, year: int) -> int:
    import calendar
    mi = MONTHS_ES.index(month.lower()) + 1
    return calendar.monthrange(year, mi)[1]


def parse_pdf(pdf_path: Path, ndays: int | None = None) -> list[dict]:
    """Parse a CENAGAS monthly volume PDF into per-node daily series.

    Layout (one row per commercial node):
        {CODE} MMpcd {d1} {d2} ... {dN} {Total}
    Rows can wrap across lines, so we split the full text on the code tokens and
    collect every float in each node's block (terminated by the next code). The
    first `ndays` floats are the daily values; the next is the printed monthly
    total. We validate sum(daily) ~= total and flag rows that don't reconcile,
    so a misparse (wrapped/merged row) is caught instead of silently loaded.
    """
    try:
        import pypdf
    except ImportError:
        raise SystemExit("Install pypdf: pip install pypdf")

    reader = pypdf.PdfReader(str(pdf_path))
    full_text = "\n".join(p.extract_text() for p in reader.pages)

    # Split into per-node blocks: each starts at a code token "E016 MMpcd".
    code_re = re.compile(r"\b([EN]\d{3,4})\s+MMpcd\b")
    matches = list(code_re.finditer(full_text))
    num_re = re.compile(r"-?\d+(?:\.\d+)?")
    out: list[dict] = []
    for i, m in enumerate(matches):
        code = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        block = full_text[start:end]
        nums = [float(n) for n in num_re.findall(block)]
        if len(nums) < 2:
            continue

        daily: list[float]
        total: float
        reconciled: bool
        if ndays is not None and len(nums) >= ndays + 1:
            daily = nums[:ndays]
            total = nums[ndays]
            reconciled = abs(sum(daily) - total) <= max(1.0, 0.02 * abs(total))
        else:
            # Unknown day count or short row: treat last float as the total.
            daily = nums[:-1]
            total = nums[-1]
            reconciled = abs(sum(daily) - total) <= max(1.0, 0.02 * abs(total))

        out.append({
            "code": code,
            "daily_mmcfd": daily,
            "monthly_total_mmcfd": total,
            "reconciled": reconciled,
            "info": CENAGAS_POINT_MAP.get(code, {}),
        })
    return out


def load_to_db(rows: list[dict], year: int, month: str) -> int:
    """Upsert VERIFIED, reconciled CENAGAS nodes into feedgas.db as daily rows.

    Only nodes present in CENAGAS_POINT_MAP with verified=True AND a `terminal`
    are loaded — we never write a node whose US-side crossing is a guess. Each
    daily value becomes a FlowRecord with cycle="monthly" so it never collides
    with the daily EBB cycles (the dashboard/stats default to "evening").
    """
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from datetime import date
    from ng_feedgas.models import FlowRecord
    from ng_feedgas.storage.db import connect, upsert_flows

    mi = MONTHS_ES.index(month.lower()) + 1
    src = ("https://boletin-gestor.cenagas.gob.mx/Docs/Volumen/"
           f"Volumen_{month.lower()}_{year}.pdf")
    records: list[FlowRecord] = []
    skipped: list[str] = []
    for r in rows:
        info = r["info"]
        if not (info.get("verified") and info.get("terminal")):
            continue
        if not r["reconciled"]:
            skipped.append(f"{r['code']} (did not reconcile)")
            continue
        for day_idx, val in enumerate(r["daily_mmcfd"], start=1):
            try:
                gd = date(year, mi, day_idx)
            except ValueError:
                continue
            records.append(FlowRecord(
                gas_day=gd,
                cycle="monthly",
                terminal=info["terminal"],
                pipeline=info.get("pipeline", r["code"]),
                meter_point=f"CENAGAS {r['code']}",
                mmcfd=float(val),
                direction="delivery",   # US -> Mexico export
                source_url=src,
            ))
    if skipped:
        print(f"  WARN: skipped non-reconciling verified nodes: {skipped}")
    if not records:
        print("  No verified+reconciled mapped nodes to load. "
              "Populate CENAGAS_POINT_MAP with verified=True entries first.")
        return 0
    with connect() as conn:
        n = upsert_flows(conn, records)
    print(f"  Loaded {n} daily rows (cycle='monthly') for "
          f"{len({rec.terminal for rec in records})} terminals.")
    return n


def fetch_import_catalog() -> list[dict]:
    """Fetch CENAGAS's commercial-node catalog and return the import (V) nodes.

    Uses cert verification disabled (CENAGAS's chain doesn't validate), same as
    download_pdf(). Returns [{code, name, origin, desc}] for Importación nodes.
    """
    import ssl as _ssl
    from urllib.request import Request as _Req, urlopen as _open
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise SystemExit("Install beautifulsoup4: pip install beautifulsoup4 lxml")
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    url = "https://boletin-gestor.cenagas.gob.mx/GestionComercial/Nodos"
    html = _open(_Req(url, headers={"User-Agent": "Mozilla/5.0"}), context=ctx, timeout=40).read()
    soup = BeautifulSoup(html.decode("utf-8", "replace"), "lxml")
    out: list[dict] = []
    for tr in soup.find_all("tr"):
        c = [x.get_text(" ", strip=True) for x in tr.find_all(["th", "td"])]
        if len(c) >= 3 and re.match(r"^V\w{3}$", c[0]) and any("mportaci" in str(x) for x in c):
            out.append({"code": c[0], "name": c[1], "desc": c[-1]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", action="store_true",
                    help="Fetch CENAGAS node catalog and print the US-border import "
                         "(V) node crosswalk, then exit.")
    ap.add_argument("--month", help="Spanish month name e.g. enero")
    ap.add_argument("--year", type=int)
    ap.add_argument("--show-unmapped", action="store_true",
                    help="Also show rows whose code isn't in CENAGAS_POINT_MAP")
    ap.add_argument("--load", action="store_true",
                    help="Upsert verified+reconciled mapped nodes into feedgas.db "
                         "(cycle='monthly'). No-op until nodes are marked verified=True.")
    args = ap.parse_args()

    if args.catalog:
        nodes = fetch_import_catalog()
        print(f"\nCENAGAS US-border IMPORT nodes (Origen = Importación): {len(nodes)}\n")
        print(f"{'CODE':6s} {'NAME':14s} {'TERMINAL':28s} DESCRIPTION")
        print("-" * 100)
        for n in nodes:
            xw = IMPORT_NODE_CROSSWALK.get(n["code"], {})
            term = xw.get("terminal") or ""
            desc = n["desc"].encode("ascii", "replace").decode("ascii")
            print(f"{n['code']:6s} {n['name'][:14]:14s} {term[:28]:28s} {desc[:54]}")
        print("\nNOTE: these import volumes are NOT in the monthly extraction PDF; "
              "no free downloadable source of their daily volumes is known.")
        return

    if not (args.month and args.year):
        raise SystemExit("Provide --month and --year (or --catalog).")

    pdf = download_pdf(args.month, args.year)
    ndays = _days_in_month(args.month, args.year)
    rows = parse_pdf(pdf, ndays=ndays)
    print(f"\nExtracted {len(rows)} commercial-node rows from {pdf.name} "
          f"({ndays} days in month)")
    n_bad = sum(1 for r in rows if not r["reconciled"])
    print(f"Reconciliation: {len(rows) - n_bad}/{len(rows)} rows sum~=total; "
          f"{n_bad} flagged (likely wrapped/merged rows).")
    print()
    print(f"{'CODE':<5} {'OK':<3} {'PIPELINE':<25} {'VERIFIED':<9} {'DAYS':>4} {'AVG':>7} {'MAX':>7} {'TOTAL':>9}")
    print("-" * 84)
    for r in rows:
        info = r["info"]
        if not info and not args.show_unmapped:
            continue
        daily = r["daily_mmcfd"]
        avg = sum(daily) / len(daily) if daily else 0
        peak = max(daily) if daily else 0
        print(
            f"{r['code']:<5} "
            f"{'OK' if r['reconciled'] else '!!':<3} "
            f"{(info.get('pipeline') or '(unmapped)'):<25} "
            f"{('yes' if info.get('verified') else 'NO'):<9} "
            f"{len(daily):>4} "
            f"{avg:>7.1f} "
            f"{peak:>7.1f} "
            f"{r['monthly_total_mmcfd']:>9.1f}"
        )

    if args.load:
        print("\nLoading verified nodes into feedgas.db ...")
        load_to_db(rows, args.year, args.month)


if __name__ == "__main__":
    main()
