"""Configuration loader."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent / "meter_points.yaml"


@dataclass(frozen=True)
class MeterPoint:
    terminal: str
    state: str
    nameplate_mmcfd: float
    scraper: str
    pipeline: str
    pipeline_code: str
    meter_id: str | None
    location_name: str
    direction: str


def categorize(terminal: str) -> str:
    """Classify a terminal by its name prefix.

    Single source of truth shared by the CLI export, stats, and the dashboard
    so the per-terminal lines and totals can never disagree about what counts as
    'U.S. LNG' vs a cross-border point.
    """
    if terminal.startswith("Mexico"):
        return "Mexico exports"
    if terminal.startswith("Canada"):
        return "Canada border"
    return "U.S. LNG"


# Canada border terminals whose useful flow is a RECEIPT (import) rather than a
# delivery. Used by direction-aware totals so imports aren't dropped/mislabelled.
CANADA_IMPORT_TERMINALS = {
    "Canada - Sumas (Northwest)",
    "Canada - Waddington (Iroquois)",
    "Canada - Emerson (Viking/GreatLakes/NorthernBorder)",
}


@dataclass(frozen=True)
class Config:
    meter_points: list[MeterPoint]
    us_total_min_mmcfd: float
    us_total_max_mmcfd: float
    terminal_nameplate: dict[str, float]

    def for_scraper(self, scraper_name: str) -> list[MeterPoint]:
        return [m for m in self.meter_points if m.scraper == scraper_name]

    def for_pipeline_code(self, scraper_name: str, pipeline_code: str) -> list[MeterPoint]:
        return [
            m for m in self.meter_points
            if m.scraper == scraper_name and m.pipeline_code == pipeline_code
        ]

    def terminals_in_order(self) -> list[str]:
        """Configured terminal names in YAML order (deduped)."""
        return list(self.terminal_nameplate.keys())

    def lng_terminals(self) -> list[str]:
        """LNG-export terminal names in YAML order."""
        return [t for t in self.terminals_in_order() if categorize(t) == "U.S. LNG"]


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    meter_points: list[MeterPoint] = []
    nameplate: dict[str, float] = {}
    for term in raw.get("terminals", []):
        nameplate[term["name"]] = float(term["nameplate_mmcfd"])
        for feed in term.get("feeds", []) or []:
            meter_points.append(MeterPoint(
                terminal=term["name"],
                state=term["state"],
                nameplate_mmcfd=float(term["nameplate_mmcfd"]),
                scraper=feed["scraper"],
                pipeline=feed["pipeline"],
                pipeline_code=feed["pipeline_code"],
                meter_id=feed.get("meter_id"),
                location_name=feed["location_name"],
                direction=feed["direction"],
            ))
    bounds = raw.get("us_total_bounds_mmcfd", {})
    return Config(
        meter_points=meter_points,
        us_total_min_mmcfd=float(bounds.get("min", 8000)),
        us_total_max_mmcfd=float(bounds.get("max", 13000)),
        terminal_nameplate=nameplate,
    )
