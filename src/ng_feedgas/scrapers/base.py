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
        self._user_agent = user_agent
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    @abstractmethod
    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        """Pull scheduled-quantity records for all configured meters."""

    def _sleep(self, ctx: ScrapeContext) -> None:
        time.sleep(ctx.request_delay_s)

    def _reset_session(self) -> None:
        """Start a clean HTTP session (clears cookies that a portal may have flagged)."""
        ua = self.session.headers.get("User-Agent", self._user_agent)
        try:
            self.session.close()
        except Exception:
            pass
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua})

    # Transient errors worth retrying. Parse/schema errors (ParseError) are NOT
    # here on purpose: if a portal has no data for the requested day, retrying
    # won't help and would just waste backoff time on every run.
    RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
        ConnectionError,
        TimeoutError,
    )

    def with_retry(self, fn, *, label: str | None = None,
                   attempts: int = 4, base_backoff_s: float = 3.0,
                   retry_on: tuple | None = None):
        """Run fn() with exponential backoff, resetting the session between tries.

        Portals intermittently drop connections (RemoteDisconnected), rate-limit,
        or time out. This makes a single transient hiccup non-fatal instead of
        silently losing the day's data for a scraper. Originally KMI-only; lifted
        here so every scraper can opt in. Only transient errors (RETRYABLE) are
        retried; ParseError/ScraperError propagate immediately.
        """
        label = label or self.name
        retry_on = retry_on or self.RETRYABLE
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except retry_on as exc:
                last_exc = exc
                if attempt < attempts:
                    backoff = base_backoff_s * (2 ** (attempt - 1))
                    log.warning("%s: attempt %d/%d failed (%s); retrying in %.0fs",
                                label, attempt, attempts, exc, backoff)
                    time.sleep(backoff)
                    self._reset_session()
        assert last_exc is not None
        raise last_exc
