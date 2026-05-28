"""Seed the AIS registry with a vetted LNG-carrier fleet list.

Loads data/lng_fleet.csv (header: mmsi,name,length_m,width_m) into ais_ships,
flagging each row is_lng_carrier=1 so a single AIS position report classifies the
vessel without needing to capture its static broadcast.

There is no free bulk LNG-fleet dataset, so populate data/lng_fleet.csv from a
source you trust (e.g. an export from MarineTraffic/VesselFinder/Datalastic, or a
list you maintain). DO NOT guess MMSIs — a wrong MMSI permanently mislabels a real
vessel. The registry ALSO self-seeds from observed calls, so this file can start
small and grow; it is purely an accelerator for instant coverage.

Run:  python tools/seed_lng_fleet.py            # loads data/lng_fleet.csv
      python tools/seed_lng_fleet.py path.csv   # loads a specific file
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ng_feedgas.calibration.ais import seed_fleet_from_csv  # noqa: E402
from ng_feedgas.storage.db import connect                   # noqa: E402

DEFAULT_CSV = ROOT / "data" / "lng_fleet.csv"


def main() -> None:
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}\n"
              f"Create it with header: mmsi,name,length_m,width_m")
        sys.exit(1)
    with connect() as conn:
        loaded, skipped = seed_fleet_from_csv(conn, csv_path)
    print(f"Loaded {loaded} carriers, skipped {skipped} bad rows from {csv_path.name}")


if __name__ == "__main__":
    main()
