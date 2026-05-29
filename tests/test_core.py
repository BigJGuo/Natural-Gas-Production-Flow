"""Unit tests for the pure functions that the data pipeline depends on.

Run:  PYTHONPATH=src python -m pytest tests/ -q
These are deliberately dependency-free (no network, no Playwright) so CI can run
them in seconds. The TETCO selection test in particular guards against the
wrong-gas-day regression that the audit flagged.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from ng_feedgas.scrapers import kmi, williams, enbridge, tcplus, trellis, empire  # noqa: E402
from ng_feedgas.scrapers.enbridge import select_tetco_option     # noqa: E402
from ng_feedgas.scrapers.base import ScrapeContext               # noqa: E402
from ng_feedgas.config import MeterPoint                          # noqa: E402
from ng_feedgas.models import FlowRecord                          # noqa: E402
from ng_feedgas.storage.db import upsert_flows, terminal_totals  # noqa: E402
from ng_feedgas.config import categorize                          # noqa: E402
from ng_feedgas.cli import _auto_cycle, FAST_SCRAPERS, SLOW_SCRAPERS  # noqa: E402
from ng_feedgas.calibration.ais import classify_lng_carrier, is_known_small  # noqa: E402


# ---------- number parsing ----------

@pytest.mark.parametrize("raw,expected", [
    ("1,234", 1234.0),
    ("786,529", 786529.0),
    ("-50", -50.0),
    ("12.5", 12.5),
    ("  3,000  ", 3000.0),
    ("", None),
    ("n/a", None),
    (None, None),
])
def test_kmi_parse_number(raw, expected):
    assert kmi._parse_number(raw) == expected


def test_williams_parse_number_matches_kmi():
    for raw in ("1,000", "-7", "9.9", "junk", ""):
        assert williams._parse_number(raw) == kmi._parse_number(raw)


# ---------- cycle mappings ----------

def test_kmi_cycle_map_covers_all_cycles():
    for c in ("timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"):
        assert c in kmi.CYCLE_TO_KMI


def test_williams_cycle_map_covers_all_cycles():
    for c in ("timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"):
        assert c in williams.CYCLE_TO_WILLIAMS


def test_auto_cycle_returns_valid_cycle():
    assert _auto_cycle() in {"evening", "intraday1", "intraday2", "intraday3"}


def test_fast_slow_scrapers_disjoint():
    assert set(FAST_SCRAPERS).isdisjoint(SLOW_SCRAPERS)
    assert "tceconnects" in SLOW_SCRAPERS and "iroquois" in SLOW_SCRAPERS


# ---------- TETCO option selection (the audited regression) ----------

def test_tetco_picks_same_day_preferred_prefix():
    opts = ["TIMELY_2026-05-27_1504", "LATEC_2026-05-28_0900", "TIMELY_2026-05-28_0600"]
    assert select_tetco_option(opts, "LATEC", "2026-05-28") == "LATEC_2026-05-28_0900"


def test_tetco_falls_back_to_same_day_any_prefix():
    # No LATEC for the day, but a TIMELY snapshot for the SAME day exists.
    opts = ["TIMELY_2026-05-27_1504", "TIMELY_2026-05-28_0600"]
    assert select_tetco_option(opts, "LATEC", "2026-05-28") == "TIMELY_2026-05-28_0600"


def test_tetco_returns_none_when_no_option_for_requested_day():
    # Regression guard: portal only has PRIOR-day snapshots -> must NOT pick one.
    opts = ["TIMELY_2026-05-27_1504", "TIMELY_2026-05-27_0900"]
    assert select_tetco_option(opts, "LATEC", "2026-05-28") is None


# ---------- categorize ----------

def test_categorize():
    assert categorize("Sabine Pass") == "U.S. LNG"
    assert categorize("Mexico - Sasabe (Sierrita)") == "Mexico exports"
    assert categorize("Canada - Sumas (Northwest)") == "Canada border"


# ---------- upsert idempotency ----------

def _mk_record(mmcfd: float, gas_day=date(2026, 5, 28)) -> FlowRecord:
    return FlowRecord(
        gas_day=gas_day, cycle="evening", terminal="Sabine Pass",
        pipeline="NGPL", meter_point="SPLIQ", mmcfd=mmcfd, direction="delivery",
        source_url="http://x", scraped_at=datetime.now(timezone.utc),
    )


def _schema(conn):
    schema = (SRC / "ng_feedgas" / "storage" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)


def test_upsert_is_idempotent_and_updates_in_place():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    # Same (gas_day, cycle, pipeline, meter_point) inserted twice with new value
    upsert_flows(conn, [_mk_record(100.0)])
    upsert_flows(conn, [_mk_record(250.0)])
    rows = conn.execute("SELECT mmcfd FROM flows").fetchall()
    assert len(rows) == 1            # no duplicate
    assert rows[0]["mmcfd"] == 250.0  # latest value wins
    # terminal_totals sums delivery
    totals = terminal_totals(conn, date(2026, 5, 28), "evening")
    assert totals["Sabine Pass"] == 250.0


def test_upsert_empty_is_noop():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    assert upsert_flows(conn, []) == 0


# ---------- AIS LNG-carrier classification ----------

def test_classify_flagged_carrier_without_size():
    # The key win: a registry-flagged MMSI classifies even with no size/type.
    assert classify_lng_carrier(None, None, None, True) is True


def test_classify_by_size():
    assert classify_lng_carrier(80, 295, 47, False) is True     # UMM SWAYYAH
    assert classify_lng_carrier(0, 290, 43, False) is True      # conventional LNGC


def test_classify_barge_excluded():
    # 209x23 ATB: long but narrow, not flagged, type 57 -> NOT a carrier.
    assert classify_lng_carrier(57, 209, 23, False) is False


def test_classify_tanker_type_fallback():
    # Tanker type, size unknown -> counted (tanker-at-berth fallback).
    assert classify_lng_carrier(80, None, None, False) is True
    assert classify_lng_carrier(84, 0, 0, False) is True


def test_classify_tug_excluded():
    assert classify_lng_carrier(52, None, None, False) is False
    assert is_known_small(52, None) is True


def test_known_small_keeps_unknown():
    # Unknown size + unknown type must NOT be dropped (we still want to learn it).
    assert is_known_small(0, None) is False
    assert is_known_small(None, None) is False


# ---------- tcplus (TC Energy Ganesha EBB) scraper ----------

def test_tcplus_cycle_map_covers_all_cycles():
    for c in ("timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"):
        assert c in tcplus.CYCLE_TYPE


def test_tcplus_dir_mapping():
    assert tcplus._dir("R") == "receipt"
    assert tcplus._dir("D") == "delivery"
    assert tcplus._dir("BD") is None      # bidirectional -> caller uses configured dir
    assert tcplus._dir("") is None


def test_tcplus_parse_number():
    assert tcplus._parse_number("2,008,731") == 2008731.0
    assert tcplus._parse_number("-161748") == -161748.0
    assert tcplus._parse_number("") is None
    assert tcplus._parse_number("-") is None


def _tcplus_meter(meter_id, terminal, direction, code="GTN"):
    return MeterPoint(
        terminal=terminal, state="ID", nameplate_mmcfd=2900.0, scraper="tcplus",
        pipeline="GTN", pipeline_code=code, meter_id=meter_id,
        location_name=terminal, direction=direction,
    )


def test_tcplus_match_meters_uses_loc_flowind_and_scales():
    import pandas as pd
    # Mimics the post-strip OAC CSV: TSQ in MMBtu/d; /1000 -> MMcf/d.
    df = pd.DataFrame({
        "Loc Name": ["KINGSGATE", "ST CLAIR DELIVERY", "ST CLAIR RECEIPT"],
        "Loc": ["3498", "11772", "710019"],
        "Flow Ind": ["R", "D", "R"],
        "TSQ": ["2,008,731", "644,695", "484,947"],
    })
    ctx = ScrapeContext(gas_day=date(2026, 5, 29), cycle="evening", meter_points=[])
    meters = [
        _tcplus_meter("3498", "Canada - Kingsgate (GTN)", "receipt"),
        _tcplus_meter("11772", "Canada - St. Clair (Great Lakes export)", "delivery", code="GLGT"),
    ]
    recs = tcplus._match_meters(df, meters, ctx, "http://x")
    by_term = {r.terminal: r for r in recs}
    assert by_term["Canada - Kingsgate (GTN)"].mmcfd == pytest.approx(2008.731)
    assert by_term["Canada - Kingsgate (GTN)"].direction == "receipt"
    assert by_term["Canada - St. Clair (Great Lakes export)"].mmcfd == pytest.approx(644.695)
    assert by_term["Canada - St. Clair (Great Lakes export)"].direction == "delivery"


# ---------- trellis (DT Midstream / Viking) scraper ----------

def test_trellis_cycle_map_covers_all_cycles():
    for c in ("timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"):
        assert c in trellis.CYCLE_TO_TRELLIS


def test_trellis_dir_mapping():
    assert trellis._dir("R") == "receipt"
    assert trellis._dir("D") == "delivery"
    assert trellis._dir("BD") is None


def test_trellis_match_meters_parses_xml_rows_and_scales():
    # Mimics the parsed columnNames + <cell> rows from getInfoPostRptTxtFile.do.
    cols = ["loc_hidden", "Loc Name", "Loc", "Loc Prop", "Loc Purp Desc",
            "Flow Ind", "Loc/QTI", "All Qty Avail", "DC", "OPC", "TSQ", "OAC", "IT", "Qty Reason"]
    rows = [
        ["2", "Ada", "11975", "11975", "Delivery Location", "D", "DPQ", "N", "1896", "1896", "0", "1896", "N", ""],
        ["30", "Emerson", "33973", "33973", "Receipt Location", "R", "RPQ", "Y", "891750", "891750", "400199", "491551", "Y", ""],
    ]
    ctx = ScrapeContext(gas_day=date(2026, 5, 29), cycle="evening", meter_points=[])
    meters = [MeterPoint(terminal="Canada - Emerson (GreatLakes/Viking)", state="MN",
                         nameplate_mmcfd=4000.0, scraper="trellis", pipeline="Viking",
                         pipeline_code="VGT", meter_id="33973", location_name="Emerson",
                         direction="receipt")]
    recs = trellis._match_meters(cols, rows, meters, ctx, "http://x")
    assert len(recs) == 1
    assert recs[0].mmcfd == pytest.approx(400.199)
    assert recs[0].direction == "receipt"


# ---------- empire (National Fuel) scraper ----------

def test_empire_cycle_map_has_evening_and_timely():
    assert empire.CYCLE_TO_BTN["evening"] == "0"
    assert empire.CYCLE_TO_BTN["timely"] == "1"


def test_empire_dir_mapping():
    assert empire._dir("Receipt") == "receipt"
    assert empire._dir("Delivery") == "delivery"
    assert empire._dir("R") == "receipt"
    assert empire._dir("") is None


def test_empire_match_meters_picks_direction_and_scales():
    import pandas as pd
    df = pd.DataFrame({
        "Loc Name": ["TCPL - Niagara*", "TCPL - Niagara*"],
        "Loc": ["421079", "421079"],
        "Total Scheduled Quantity": ["0", "315339"],
        "Flow Indicator": ["Receipt", "Delivery"],
    })
    ctx = ScrapeContext(gas_day=date(2026, 5, 29), cycle="evening", meter_points=[
        MeterPoint(terminal="Canada - Chippawa (Empire)", state="NY", nameplate_mmcfd=850.0,
                   scraper="empire", pipeline="Empire", pipeline_code="EMPIRE",
                   meter_id="421079", location_name="TCPL - Niagara", direction="delivery"),
    ])
    recs = empire._match_meters(df, ctx, "http://x")
    assert len(recs) == 1
    assert recs[0].mmcfd == pytest.approx(315.339)   # picks the Delivery row, not the 0 receipt
    assert recs[0].direction == "delivery"


# ---------- CENAGAS monthly PDF parser ----------

def _load_cenagas_module():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "tools" / "cenagas_pdf_backfill.py"
    spec = importlib.util.spec_from_file_location("cenagas_pdf_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CENAGAS_PDF = (Path(__file__).resolve().parent.parent
               / "data" / "cenagas" / "Volumen_enero_2026.pdf")


@pytest.mark.skipif(not CENAGAS_PDF.exists(), reason="CENAGAS sample PDF not present")
def test_cenagas_parse_reconciles_clean_rows():
    pytest.importorskip("pypdf")
    mod = _load_cenagas_module()
    rows = mod.parse_pdf(CENAGAS_PDF, ndays=31)
    assert len(rows) > 100
    by_code = {r["code"]: r for r in rows}
    # E016 is a clean (non-wrapping) row: 31 daily values that sum to its total.
    e016 = by_code["E016"]
    assert len(e016["daily_mmcfd"]) == 31
    assert e016["reconciled"] is True
    assert sum(e016["daily_mmcfd"]) == pytest.approx(e016["monthly_total_mmcfd"], rel=0.02)
    # The majority of rows must reconcile (wrapped rows are flagged, not silently kept).
    assert sum(1 for r in rows if r["reconciled"]) >= 0.6 * len(rows)


@pytest.mark.skipif(not CENAGAS_PDF.exists(), reason="CENAGAS sample PDF not present")
def test_cenagas_loader_refuses_unverified_nodes():
    pytest.importorskip("pypdf")
    mod = _load_cenagas_module()
    rows = mod.parse_pdf(CENAGAS_PDF, ndays=31)
    # No node is verified=True yet, so the loader must write nothing (it must never
    # load a guessed crosswalk into the DB).
    assert mod.load_to_db(rows, 2026, "enero") == 0
