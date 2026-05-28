"""Abstract base for EBB scrapers."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

import requests

from ..config import MeterPoint
from ..models import Cycle, FlowRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeContext:
    gas_day: date
    cycle: Cycle
    meter_points: list[MeterPoint]   # Filtered to those this scraper is responsible for
    request_delay_s: float = 2.5


class BaseScraper(ABC):
    name: str = "base"

    def __init__(self, user_agent: str = "ng-feedgas/0.1 (+research)"):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    @abstractmethod
    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        """Pull scheduled-quantity records for all configured meters."""

    def _sleep(self, ctx: ScrapeContext) -> None:
        time.sleep(ctx.request_delay_s)
