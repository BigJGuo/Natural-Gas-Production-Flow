"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

Cycle = Literal["timely", "evening", "intraday1", "intraday2", "intraday3", "confirmed"]
Direction = Literal["receipt", "delivery"]


@dataclass(frozen=True)
class FlowRecord:
    """A single scraped feedgas observation at one meter point."""
    gas_day: date
    cycle: Cycle
    terminal: str
    pipeline: str
    meter_point: str
    mmcfd: float
    direction: Direction
    source_url: str
    scraped_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ScraperError(Exception):
    """Raised when a scraper cannot complete a fetch (network, parse, auth)."""


class ParseError(ScraperError):
    """Raised when the scraped page doesn't match the expected schema."""
