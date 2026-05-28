"""AIS vessel tracking for LNG terminals — uses aisstream.io free WebSocket feed.

Register at https://aisstream.io for a free API key. Then set the env var:

    setx AISSTREAM_API_KEY "your-key-here"     # Windows
    export AISSTREAM_API_KEY="your-key-here"   # macOS/Linux

The function `collect(duration_s)` opens a WebSocket connection, subscribes to
bounding boxes around each LNG terminal, listens for AIS messages for the
specified duration, and writes observations to the ais_observations table.

A separate function `infer_daily_activity(gas_day)` reads the observations and
emits a crude per-terminal feedgas estimate based on whether LNG carriers were
moored at the terminal during that day.

Run as a one-shot from the CLI:
    python -m ng_feedgas.calibration.ais collect --duration 60     # listen 60 seconds
    python -m ng_feedgas.calibration.ais infer --date today        # roll up to daily
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

_GO_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def _normalize_ts(raw) -> str:
    """Normalize aisstream's Go-style timestamp to ISO 8601 (UTC).

    aisstream sends e.g. '2026-05-28 14:21:11.488129741 +0000 UTC'. We store
    'YYYY-MM-DDTHH:MM:SS+00:00' so string ordering and date-prefix filtering
    behave correctly (a space separator sorts before 'T', which silently broke
    the day-window query before this fix).
    """
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    m = _GO_TS_RE.match(str(raw).strip())
    if m:
        return f"{m.group(1)}T{m.group(2)}+00:00"
    return str(raw)

AISSTREAM_WS = "wss://stream.aisstream.io/v0/stream"
DEFAULT_API_KEY_ENV = "AISSTREAM_API_KEY"

# LNG terminal berth coordinates. Format: terminal -> (south_lat, west_lon, north_lat, east_lon).
# Cameron LNG and Calcasieu Pass sit ~1.5 nm apart on the Calcasieu Ship Channel,
# so their boxes are tightened to ~1 km squares around each berth and are
# DISJOINT — previously they overlapped and every Calcasieu vessel was logged as
# Cameron (dict-order first-match). With disjoint boxes a point matches at most
# one terminal, so _which_terminal's iteration order no longer matters.
LNG_TERMINAL_BOXES: dict[str, tuple[float, float, float, float]] = {
    "Sabine Pass":    (29.70, -93.92, 29.76, -93.82),
    "Corpus Christi": (27.75, -97.37, 27.83, -97.27),
    "Freeport LNG":   (28.92, -95.36, 29.00, -95.26),
    "Cameron LNG":    (29.789, -93.336, 29.799, -93.326),   # ~29.794,-93.331 berth
    "Cove Point":     (38.36, -76.43, 38.42, -76.35),
    "Elba Island":    (32.01, -80.98, 32.07, -80.88),
    "Calcasieu Pass": (29.776, -93.348, 29.786, -93.338),   # ~29.781,-93.343 berth
    "Plaquemines":    (29.34, -89.67, 29.42, -89.57),
}

# LNG carrier classification.
#
# AIS type 80-89 covers ALL tankers (oil, chemical, product, gas), so type alone
# is too broad. Size is the reliable discriminator: an LNG carrier is ~285-345m
# LOA and beamy (43-55m). A 250m+ AND 38m+ hull is essentially always an LNG
# carrier; nothing else that shape berths at these export terminals. The length
# floor was raised 200->250m and a beam floor added so long-but-narrow barges
# (e.g. a 209x23m ATB) no longer trip the filter.
LNG_TANKER_TYPES = {80, 84}      # gas tanker (80); hazmat-D (84) occasionally used
LNG_MIN_LENGTH_M = 250.0
LNG_MIN_BEAM_M = 38.0

# "Possible LNG" net for the SCANNER (looser than the loading test above): we
# only store/show vessels that could plausibly be an LNG carrier and drop the
# harbor fleet (tugs, pilots, crew boats). A vessel is kept unless we KNOW it is
# small — confirmed length < 150m, or a small-craft AIS type. Unknown-size
# vessels are kept so the registry can still learn them (size/type often arrives
# after the first position reports).
POSSIBLE_LNG_MIN_LENGTH_M = 150.0
# Fishing/towing/dredging/military/sailing/pleasure (30-37) + pilot/tug/port-
# tender/SAR/law/medical/noncombatant (50-59, excluding 56/57 "spare-local"
# which are sometimes mis-set on larger hulls).
SMALL_CRAFT_TYPES = frozenset({30, 31, 32, 33, 34, 35, 36, 37,
                               50, 51, 52, 53, 54, 55, 58, 59})


def is_known_small(ship_type, length_m) -> bool:
    """True if we can already rule this vessel out as an LNG carrier.

    Conservative: returns False when size/type are unknown, so a not-yet-sized
    vessel is never dropped before we learn what it is.
    """
    try:
        if length_m is not None and 0 < float(length_m) < POSSIBLE_LNG_MIN_LENGTH_M:
            return True
    except (TypeError, ValueError):
        pass
    return ship_type in SMALL_CRAFT_TYPES


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def classify_lng_carrier(ship_type, length_m, width_m, is_flagged) -> bool:
    """Single source of truth: is this an LNG carrier loading at berth?

    Order of evidence:
      1. is_flagged — registry already confirmed this MMSI (self-seeded from a
         prior observation, or imported from a vetted fleet CSV). Lets a bare
         position report classify the vessel without re-capturing static data.
      2. Size — a large, beamy hull (>=250m AND >=38m beam) is unambiguously an
         LNG carrier; nothing else that shape berths at these export terminals.
      3. Tanker type (80/84) — fallback for a vessel typed but not yet sized.
    """
    if is_flagged:
        return True
    length, width = _num(length_m), _num(width_m)
    if length >= LNG_MIN_LENGTH_M and width >= LNG_MIN_BEAM_M:
        return True
    return ship_type in LNG_TANKER_TYPES

# Default nameplate for "ship at berth = active loading" inference
# Pulled lazily from meter_points.yaml at runtime.
def _terminal_nameplates() -> dict[str, float]:
    try:
        from ..config import load_config
        cfg = load_config()
        return dict(cfg.terminal_nameplate)
    except Exception:
        return {}


def get_api_key() -> str | None:
    return os.environ.get(DEFAULT_API_KEY_ENV)


# ---------- collector ----------

async def _collect_async(api_key: str, duration_s: int, conn: sqlite3.Connection) -> int:
    """Connect to AIS stream, listen for duration_s seconds, write to DB."""
    import websockets   # local import — heavy

    # Build bounding boxes in the format aisstream.io expects:
    # [[[lat_min, lon_min], [lat_max, lon_max]], ...]
    boxes = [
        [[s_lat, w_lon], [n_lat, e_lon]]
        for s_lat, w_lon, n_lat, e_lon in LNG_TERMINAL_BOXES.values()
    ]
    sub = {
        "APIKey": api_key,
        "BoundingBoxes": boxes,
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    log.info("AIS: opening WebSocket to %s (duration %ds, %d boxes)",
             AISSTREAM_WS, duration_s, len(boxes))
    inserts = 0
    skipped_small = 0
    static_lookup: dict[int, dict] = {}   # mmsi -> latest static data (name, type)

    # Vessels the registry already knows are too small / wrong-type to be LNG
    # carriers. We skip storing their positions; unknown vessels stay (we learn
    # them as their static data arrives, then add them here mid-run).
    known_small: set[int] = set()
    for r in conn.execute("SELECT mmsi, ship_type, length_m FROM ais_ships").fetchall():
        if is_known_small(r[1], r[2]):
            known_small.add(int(r[0]))

    async with websockets.connect(AISSTREAM_WS, max_size=None) as ws:
        await ws.send(json.dumps(sub))
        log.info("AIS: subscription sent, listening...")
        deadline = datetime.now(timezone.utc) + timedelta(seconds=duration_s)

        while datetime.now(timezone.utc) < deadline:
            try:
                remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=max(1, remaining))
            except asyncio.TimeoutError:
                break
            except websockets.exceptions.ConnectionClosed:
                log.warning("AIS: connection closed by server")
                break

            try:
                msg = json.loads(msg_raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("MessageType")
            if mtype == "ShipStaticData":
                meta = msg.get("MetaData", {})
                payload = msg.get("Message", {}).get("ShipStaticData", {})
                mmsi = meta.get("MMSI")
                if mmsi:
                    name = (payload.get("Name") or meta.get("ShipName", "")).strip()
                    stype = payload.get("Type") or 0
                    dim = payload.get("Dimension", {}) or {}
                    length = (dim.get("A", 0) or 0) + (dim.get("B", 0) or 0)
                    width = (dim.get("C", 0) or 0) + (dim.get("D", 0) or 0)
                    static_lookup[int(mmsi)] = {"name": name, "type": stype}
                    # Self-seed: flag as a carrier if this observation qualifies.
                    # MAX() in the upsert means a confirmed carrier is never un-flagged
                    # by a later partial/dimensionless broadcast.
                    carrier_flag = int(classify_lng_carrier(stype, length, width, False))
                    conn.execute(
                        """
                        INSERT INTO ais_ships
                            (mmsi, ship_name, ship_type, length_m, width_m, is_lng_carrier, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(mmsi) DO UPDATE SET
                            ship_name      = excluded.ship_name,
                            ship_type      = excluded.ship_type,
                            length_m       = excluded.length_m,
                            width_m        = excluded.width_m,
                            is_lng_carrier = MAX(ais_ships.is_lng_carrier, excluded.is_lng_carrier),
                            updated_at     = excluded.updated_at
                        """,
                        (int(mmsi), name, stype, float(length), float(width),
                         carrier_flag, datetime.now(timezone.utc).isoformat()),
                    )
                    # Update the live "too small to be LNG" set as we learn sizes.
                    if is_known_small(stype, length):
                        known_small.add(int(mmsi))
                    else:
                        known_small.discard(int(mmsi))

            elif mtype == "PositionReport":
                meta = msg.get("MetaData", {})
                pos = msg.get("Message", {}).get("PositionReport", {})
                mmsi = meta.get("MMSI")
                lat = meta.get("latitude")
                lon = meta.get("longitude")
                if mmsi is None or lat is None or lon is None:
                    continue
                # Scanner filter: skip vessels we already know are small/non-LNG.
                # Unknown vessels are kept (not yet in known_small).
                if int(mmsi) in known_small:
                    skipped_small += 1
                    continue
                # Which terminal box does this position fall in?
                terminal = _which_terminal(lat, lon)
                if not terminal:
                    continue

                ship_meta = static_lookup.get(int(mmsi), {})
                ts = _normalize_ts(meta.get("time_utc"))

                conn.execute(
                    """
                    INSERT OR IGNORE INTO ais_observations
                        (terminal, captured_at, mmsi, ship_name, ship_type,
                         nav_status, sog, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        terminal, ts, int(mmsi),
                        ship_meta.get("name", "") or meta.get("ShipName", ""),
                        ship_meta.get("type"),
                        pos.get("NavigationalStatus"),
                        pos.get("Sog"),
                        float(lat), float(lon),
                    ),
                )
                inserts += 1

    conn.commit()
    log.info("AIS: wrote %d observations (skipped %d known-small vessel positions)",
             inserts, skipped_small)
    return inserts


def _which_terminal(lat: float, lon: float) -> str | None:
    for name, (s, w, n, e) in LNG_TERMINAL_BOXES.items():
        if s <= lat <= n and w <= lon <= e:
            return name
    return None


def collect(duration_s: int = 60, api_key: str | None = None, conn: sqlite3.Connection | None = None) -> int:
    """Synchronously collect AIS observations for `duration_s` seconds.

    Returns the number of observations written to the DB.
    """
    key = api_key or get_api_key()
    if not key:
        raise ValueError(
            f"No AISSTREAM API key. Get one free at https://aisstream.io and "
            f"set the {DEFAULT_API_KEY_ENV!r} env var."
        )
    own_conn = False
    if conn is None:
        from ..storage.db import DEFAULT_DB_PATH
        DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DEFAULT_DB_PATH, timeout=30)
        # Match storage.db.connect(): coexist with the intraday pull writers.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        own_conn = True
    try:
        _ensure_schema(conn)
        return asyncio.run(_collect_async(key, duration_s, conn))
    finally:
        if own_conn:
            conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    # Idempotent migrations for DBs created before these columns existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ais_ships)")}
    for col, decl in (("length_m", "REAL"), ("width_m", "REAL"),
                      ("is_lng_carrier", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE ais_ships ADD COLUMN {col} {decl}")
    conn.commit()


# ---------- inference ----------

def infer_daily_activity(gas_day: date, conn: sqlite3.Connection) -> pd.DataFrame:
    """For each LNG terminal, compute a crude feedgas estimate from AIS observations.

    An LNG carrier is a moored (sog<1 or nav_status=5) vessel with a large, beamy
    hull (length >= LNG_MIN_LENGTH_M and beam >= LNG_MIN_BEAM_M, or a gas-tanker
    type). If one was at berth that day, treat the terminal as actively loading and
    estimate feedgas at 60% of nameplate. Otherwise 0. The 60% factor is a coarse
    "is it loading" proxy, not a measurement — calibrate against EIA over time.
    """
    _ensure_schema(conn)
    day_prefix = gas_day.isoformat()   # 'YYYY-MM-DD'

    nameplates = _terminal_nameplates()
    rows = []
    now = datetime.now(timezone.utc).isoformat()

    for terminal in LNG_TERMINAL_BOXES.keys():
        # Date-prefix match is robust to timestamp separator (space vs 'T').
        # COALESCE the per-obs type with the persistent ship registry so a type
        # learned in any collection run backfills this MMSI's observations.
        df = pd.read_sql_query(
            """
            SELECT o.mmsi,
                   COALESCE(o.ship_type, s.ship_type) AS ship_type,
                   s.length_m, s.width_m,
                   COALESCE(s.is_lng_carrier, 0) AS is_lng_carrier,
                   o.nav_status, o.sog, o.captured_at
            FROM ais_observations o
            LEFT JOIN ais_ships s ON s.mmsi = o.mmsi
            WHERE o.terminal = ?
              AND substr(o.captured_at, 1, 10) = ?
            """,
            conn,
            params=(terminal, day_prefix),
        )
        if df.empty:
            ship_at_berth = 0
            est = 0.0
            moored_hours = 0.0
        else:
            # Moored = stopped (sog<1) or nav_status "moored" (5)
            moored = df[(df["sog"].fillna(0) < 1.0) | (df["nav_status"] == 5)].copy()
            # LNG carrier via the shared classifier: registry flag (MMSI-only,
            # the key win), or large+beamy hull, or tanker type.
            is_carrier = moored.apply(
                lambda r: classify_lng_carrier(
                    r["ship_type"], r["length_m"], r["width_m"], bool(r["is_lng_carrier"])
                ), axis=1,
            )
            carriers = moored[is_carrier] if not moored.empty else moored
            ship_at_berth = int(carriers["mmsi"].nunique())
            # Crude moored-hours estimate: distinct hours with a carrier at berth
            if carriers.empty:
                moored_hours = 0.0
            else:
                # format="ISO8601" tolerates mixed precision (with/without
                # microseconds) so a stray non-normalized timestamp can't crash it.
                ts_parsed = pd.to_datetime(carriers["captured_at"],
                                           format="ISO8601", errors="coerce")
                moored_hours = float(ts_parsed.dt.floor("h").nunique())
            nameplate = nameplates.get(terminal, 0)
            est = (nameplate * 0.60) if ship_at_berth > 0 else 0.0

        rows.append({
            "gas_day": gas_day.isoformat(),
            "terminal": terminal,
            "ship_at_berth": ship_at_berth,
            "moored_hours": moored_hours,
            "est_feedgas_mmcfd": est,
            "computed_at": now,
        })

    out = pd.DataFrame(rows)

    # Upsert into ais_daily_inference
    conn.executemany(
        """
        INSERT INTO ais_daily_inference
            (gas_day, terminal, ship_at_berth, moored_hours, est_feedgas_mmcfd, computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(gas_day, terminal) DO UPDATE SET
            ship_at_berth = excluded.ship_at_berth,
            moored_hours = excluded.moored_hours,
            est_feedgas_mmcfd = excluded.est_feedgas_mmcfd,
            computed_at = excluded.computed_at
        """,
        [tuple(r.values) for _, r in out.iterrows()],
    )
    conn.commit()
    return out


def seed_fleet_from_csv(conn: sqlite3.Connection, csv_path: Path) -> tuple[int, int]:
    """Bulk-load a vetted LNG-carrier list into the registry (is_lng_carrier=1).

    CSV header: mmsi,name,length_m,width_m  (name/dims optional but recommended).
    Each row is validated (MMSI = 9-digit int; dims, if present, > 0); bad rows are
    skipped with a warning. Idempotent — re-importing just refreshes. Returns
    (loaded, skipped).
    """
    import csv as _csv
    _ensure_schema(conn)
    loaded = skipped = 0
    now = datetime.now(timezone.utc).isoformat()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            raw_mmsi = (row.get("mmsi") or "").strip()
            if not (raw_mmsi.isdigit() and len(raw_mmsi) == 9):
                log.warning("seed: skipping bad MMSI %r", raw_mmsi)
                skipped += 1
                continue
            name = (row.get("name") or "").strip()
            length = _num(row.get("length_m"))
            width = _num(row.get("width_m"))
            if (row.get("length_m") and length <= 0) or (row.get("width_m") and width <= 0):
                log.warning("seed: skipping %s — non-positive dimensions", raw_mmsi)
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO ais_ships
                    (mmsi, ship_name, ship_type, length_m, width_m, is_lng_carrier, updated_at)
                VALUES (?, ?, NULL, ?, ?, 1, ?)
                ON CONFLICT(mmsi) DO UPDATE SET
                    ship_name      = COALESCE(NULLIF(excluded.ship_name, ''), ais_ships.ship_name),
                    length_m       = CASE WHEN excluded.length_m > 0 THEN excluded.length_m ELSE ais_ships.length_m END,
                    width_m        = CASE WHEN excluded.width_m  > 0 THEN excluded.width_m  ELSE ais_ships.width_m END,
                    is_lng_carrier = 1,
                    updated_at     = excluded.updated_at
                """,
                (int(raw_mmsi), name, length, width, now),
            )
            loaded += 1
    conn.commit()
    log.info("seed: loaded %d carriers, skipped %d", loaded, skipped)
    return loaded, skipped


def load_ais_inference(conn: sqlite3.Connection) -> pd.DataFrame:
    _ensure_schema(conn)
    df = pd.read_sql_query("SELECT * FROM ais_daily_inference ORDER BY gas_day, terminal", conn)
    if not df.empty:
        df["gas_day"] = pd.to_datetime(df["gas_day"]).dt.date
    return df


# ---------- CLI ----------

def _parse_date(s: str) -> date:
    if s.lower() == "today":
        return date.today()
    if s.lower() == "yesterday":
        return date.today() - timedelta(days=1)
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="ng_feedgas.calibration.ais")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_col = sub.add_parser("collect", help="Listen on AIS stream for N seconds")
    p_col.add_argument("--duration", type=int, default=60)
    p_inf = sub.add_parser("infer", help="Roll up AIS observations to daily inferences")
    p_inf.add_argument("--date", default="today")

    args = parser.parse_args()
    if args.cmd == "collect":
        n = collect(duration_s=args.duration)
        print(f"AIS: {n} observations collected")
    elif args.cmd == "infer":
        from ..storage.db import DEFAULT_DB_PATH
        with sqlite3.connect(DEFAULT_DB_PATH) as conn:
            out = infer_daily_activity(_parse_date(args.date), conn)
            print(out.to_string(index=False))


if __name__ == "__main__":
    main()
