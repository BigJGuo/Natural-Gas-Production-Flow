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
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

AISSTREAM_WS = "wss://stream.aisstream.io/v0/stream"
DEFAULT_API_KEY_ENV = "AISSTREAM_API_KEY"

# LNG terminal berth coordinates (approximate, ~5 km square around the loading dock)
# Format: terminal -> (south_lat, west_lon, north_lat, east_lon)
LNG_TERMINAL_BOXES: dict[str, tuple[float, float, float, float]] = {
    "Sabine Pass":    (29.70, -93.92, 29.76, -93.82),
    "Corpus Christi": (27.75, -97.37, 27.83, -97.27),
    "Freeport LNG":   (28.92, -95.36, 29.00, -95.26),
    "Cameron LNG":    (29.76, -93.37, 29.83, -93.27),
    "Cove Point":     (38.36, -76.43, 38.42, -76.35),
    "Elba Island":    (32.01, -80.98, 32.07, -80.88),
    "Calcasieu Pass": (29.74, -93.36, 29.81, -93.27),
    "Plaquemines":    (29.34, -89.67, 29.42, -89.57),
}

# AIS Type codes that indicate LNG-carrier-shaped cargo vessels.
# Strictly: 80=tanker (gas), 81=tanker hazardous-A. But also 70-79 (cargo) catches
# misclassified LNG carriers. For LNG feedgas inference, we keep only 80-89 (tankers).
LNG_CARRIER_TYPE_RANGE = (70, 89)

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
    static_lookup: dict[int, dict] = {}   # mmsi -> latest static data (name, type)

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
                    static_lookup[int(mmsi)] = {
                        "name": (payload.get("Name") or meta.get("ShipName", "")).strip(),
                        "type": payload.get("Type") or 0,
                    }

            elif mtype == "PositionReport":
                meta = msg.get("MetaData", {})
                pos = msg.get("Message", {}).get("PositionReport", {})
                mmsi = meta.get("MMSI")
                lat = meta.get("latitude")
                lon = meta.get("longitude")
                if mmsi is None or lat is None or lon is None:
                    continue
                # Which terminal box does this position fall in?
                terminal = _which_terminal(lat, lon)
                if not terminal:
                    continue

                ship_meta = static_lookup.get(int(mmsi), {})
                ts = meta.get("time_utc") or datetime.now(timezone.utc).isoformat()

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
    log.info("AIS: wrote %d observations", inserts)
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
        conn = sqlite3.connect(DEFAULT_DB_PATH)
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
    conn.commit()


# ---------- inference ----------

def infer_daily_activity(gas_day: date, conn: sqlite3.Connection) -> pd.DataFrame:
    """For each LNG terminal, compute a crude feedgas estimate from AIS observations.

    Rule: if any LNG carrier (ship_type 70-89) was moored or slow (sog < 1 knot)
    inside the terminal's bounding box on that day, treat the terminal as actively
    loading and estimate feedgas at 60% of nameplate. Otherwise 0.
    """
    _ensure_schema(conn)
    start = datetime(gas_day.year, gas_day.month, gas_day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    nameplates = _terminal_nameplates()
    rows = []
    now = datetime.utcnow().isoformat()

    for terminal in LNG_TERMINAL_BOXES.keys():
        df = pd.read_sql_query(
            """
            SELECT mmsi, ship_type, nav_status, sog, captured_at
            FROM ais_observations
            WHERE terminal = ?
              AND captured_at >= ?
              AND captured_at < ?
            """,
            conn,
            params=(terminal, start.isoformat(), end.isoformat()),
        )
        if df.empty:
            ship_at_berth = 0
            est = 0.0
            moored_hours = 0.0
        else:
            # Filter to LNG-carrier-type ships moving slowly or stopped
            df = df[df["ship_type"].between(*LNG_CARRIER_TYPE_RANGE, inclusive="both")]
            df = df[(df["sog"].fillna(0) < 1.0) | (df["nav_status"] == 5)]
            ship_at_berth = int(df["mmsi"].nunique())
            # Crude moored-hours estimate: count distinct hours with at-berth obs
            df["captured_at"] = pd.to_datetime(df["captured_at"])
            df["hour"] = df["captured_at"].dt.floor("h")
            moored_hours = float(df["hour"].nunique())
            nameplate = nameplates.get(terminal, 0)
            # If we saw any LNG carrier moored, estimate at 60% of nameplate
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
        [tuple(r.values()) for _, r in out.iterrows()],
    )
    conn.commit()
    return out


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
