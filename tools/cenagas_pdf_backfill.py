"""CENAGAS monthly PDF backfill (Mexico cross-border imports).

CENAGAS publishes monthly per-point daily-flow PDFs at:
  https://boletin-gestor.cenagas.gob.mx/Docs/Volumen/Volumen_{month}_{year}.pdf

Caveats:
  - PDFs are released ~30 days after month-end (not daily real-time).
  - Point codes (E016, E017, N046, ...) are CENAGAS-internal — they do NOT map
    1:1 to US-side pipeline names. A point→pipeline crosswalk needs to be
    maintained separately (see CENAGAS_POINT_MAP at bottom of file).
  - Values are in MMpcd (≈ MMcf/d) — same unit as our scraped EBB data.

Use this for:
  - Backfilling historical Trans-Pecos / Comanche Trail / NET Mexico /
    Valley Crossing flows (US-side daily EBB data does NOT exist for these
    TX intrastates; this is the only free authoritative source).
  - Calibrating scraped EPNG/Sierrita Mexico-export estimates.

DO NOT use this for:
  - Same-day or yesterday's flow data — the data isn't there yet.

Run:  python tools/cenagas_pdf_backfill.py --month enero --year 2026
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


# CENAGAS commercial-node code → US pipeline mapping. PARTIAL — refine as you
# pin down the source pipelines for each entry node.
#
# Format: code → (description, US-side pipeline if known)
#
# To populate this map: cross-reference CENAGAS's "Diagrama de Conectividad"
# at /GestionTecnica/Diagramas (PDF) with the daily volumes here.
CENAGAS_POINT_MAP = {
    # E0xx codes are "extraction" nodes — gas entering SISTRANGAS from outside.
    # When the source is the US, this represents a cross-border import to MX.
    "E016": {"name": "Los Ramones (NET Mexico inlet)",     "pipeline": "NET Mexico",      "us_op": "KMI"},
    "E017": {"name": "El Encino (Trans-Pecos inlet)",      "pipeline": "Trans-Pecos",     "us_op": "Energy Transfer"},
    "E018": {"name": "Samalayuca interconnect",            "pipeline": "Samalayuca",      "us_op": "EPNG"},
    "E023": {"name": "Reynosa / Tamaulipas border",        "pipeline": "Reynosa",         "us_op": "TBD"},
    "E032": {"name": "Sásabe / Sonora (Sierrita end)",     "pipeline": "Sierrita-Sasabe", "us_op": "KMI"},
    # ... unmapped codes are present in the PDF but not yet identified
}


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


def parse_pdf(pdf_path: Path) -> list[dict]:
    try:
        import pypdf
    except ImportError:
        raise SystemExit("Install pypdf: pip install pypdf")

    reader = pypdf.PdfReader(str(pdf_path))
    full_text = "\n".join(p.extract_text() for p in reader.pages)

    # Each data row begins with a code like "E016 MMpcd 11.38 13.34 ..."
    row_re = re.compile(r"\b([EN]\d{3,4})\s+MMpcd\s+([\d.\s]+)")
    out: list[dict] = []
    for m in row_re.finditer(full_text):
        code = m.group(1)
        nums = re.findall(r"-?\d+\.\d+", m.group(2))
        if len(nums) < 2:
            continue
        # Last number is the monthly total; rest are daily values
        daily = [float(n) for n in nums[:-1]]
        total = float(nums[-1])
        out.append({
            "code": code,
            "daily_mmcfd": daily,
            "monthly_total_mmcfd": total,
            "info": CENAGAS_POINT_MAP.get(code, {}),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="Spanish month name e.g. enero")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--show-unmapped", action="store_true",
                    help="Also show rows whose code isn't in CENAGAS_POINT_MAP")
    args = ap.parse_args()

    pdf = download_pdf(args.month, args.year)
    rows = parse_pdf(pdf)
    print(f"\nExtracted {len(rows)} commercial-node rows from {pdf.name}")
    print()
    print(f"{'CODE':<5} {'PIPELINE':<25} {'US OPERATOR':<20} {'DAYS':>4} {'AVG':>7} {'MAX':>7} {'TOTAL':>9}")
    print("-" * 80)
    for r in rows:
        info = r["info"]
        if not info and not args.show_unmapped:
            continue
        daily = r["daily_mmcfd"]
        avg = sum(daily) / len(daily) if daily else 0
        peak = max(daily) if daily else 0
        print(
            f"{r['code']:<5} "
            f"{(info.get('pipeline') or '(unmapped)'):<25} "
            f"{(info.get('us_op') or ''):<20} "
            f"{len(daily):>4} "
            f"{avg:>7.1f} "
            f"{peak:>7.1f} "
            f"{r['monthly_total_mmcfd']:>9.1f}"
        )


if __name__ == "__main__":
    main()
