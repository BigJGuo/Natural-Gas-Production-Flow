-- EIA weekly LNG export totals (US-wide)
CREATE TABLE IF NOT EXISTS eia_weekly (
    week_ending     TEXT PRIMARY KEY,   -- ISO date of the Friday ending the week
    us_lng_bcfd     REAL NOT NULL,      -- US LNG exports in Bcf/d for the week
    source_url      TEXT,
    fetched_at      TEXT NOT NULL
);

-- AIS vessel observations near LNG terminals
CREATE TABLE IF NOT EXISTS ais_observations (
    id              INTEGER PRIMARY KEY,
    terminal        TEXT NOT NULL,
    captured_at     TEXT NOT NULL,      -- ISO timestamp (UTC)
    mmsi            INTEGER NOT NULL,
    ship_name       TEXT,
    ship_type       INTEGER,            -- AIS type code; 80-89 = cargo, 70-79 = LNG carrier
    nav_status      INTEGER,            -- 0=underway, 1=anchor, 5=moored
    sog             REAL,               -- speed over ground (knots)
    lat             REAL,
    lon             REAL,
    UNIQUE(terminal, captured_at, mmsi)
);

CREATE INDEX IF NOT EXISTS idx_ais_terminal_time ON ais_observations(terminal, captured_at);

-- Persistent ship registry: maps MMSI -> static data (name, type). ShipStaticData
-- is broadcast far less often than PositionReport, so a vessel's type may only be
-- learned in a later collection run. Persisting it here lets us backfill the type
-- for all observations of that MMSI, regardless of which run first saw the static data.
CREATE TABLE IF NOT EXISTS ais_ships (
    mmsi            INTEGER PRIMARY KEY,
    ship_name       TEXT,
    ship_type       INTEGER,
    length_m        REAL,               -- overall length (Dimension A+B); LNG carriers ~290-345m
    width_m         REAL,               -- beam (Dimension C+D)
    is_lng_carrier  INTEGER NOT NULL DEFAULT 0,  -- 1 once confirmed (observed size/type) or seeded
    updated_at      TEXT NOT NULL
);

-- Inferred terminal activity rollups (one row per terminal per gas day from AIS)
CREATE TABLE IF NOT EXISTS ais_daily_inference (
    gas_day         TEXT NOT NULL,
    terminal        TEXT NOT NULL,
    ship_at_berth   INTEGER NOT NULL,   -- count of distinct LNG-carrier ships seen at berth
    moored_hours    REAL,                -- estimated hours of LNG carrier moored
    est_feedgas_mmcfd REAL,              -- crude estimate based on moored_hours × nameplate / 24
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (gas_day, terminal)
);
