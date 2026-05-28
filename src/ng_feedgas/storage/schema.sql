CREATE TABLE IF NOT EXISTS flows (
    id           INTEGER PRIMARY KEY,
    gas_day      TEXT    NOT NULL,
    cycle        TEXT    NOT NULL,
    terminal     TEXT    NOT NULL,
    pipeline     TEXT    NOT NULL,
    meter_point  TEXT    NOT NULL,
    mmcfd        REAL    NOT NULL,
    direction    TEXT    NOT NULL,
    scraped_at   TEXT    NOT NULL,
    source_url   TEXT    NOT NULL,
    UNIQUE (gas_day, cycle, pipeline, meter_point)
);

CREATE INDEX IF NOT EXISTS idx_flows_day      ON flows (gas_day);
CREATE INDEX IF NOT EXISTS idx_flows_terminal ON flows (terminal, gas_day);
CREATE INDEX IF NOT EXISTS idx_flows_cycle    ON flows (cycle, gas_day);
