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

from ng_feedgas.scrapers import kmi, williams, enbridge          # noqa: E402
from ng_feedgas.scrapers.enbridge import select_tetco_option     # noqa: E402
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
