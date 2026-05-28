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
