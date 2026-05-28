"""TC Energy — Tennessee Gas Pipeline (TGP) scraper.

Important: the Kinder Morgan Pipeline Portal (`pipeline2.kindermorgan.com`)
hosts TGP's informational postings (under `code=TGP`, TSP=1939164). So this
scraper simply delegates to the KMI scraper using `pipeline_code=TGP` for
meter points configured under the `tcenergy` scraper.

If TC Energy migrates TGP off the KMI-hosted portal in the future, this is
where to add the new endpoint logic.
"""
from __future__ import annotations

import logging

from ..models import FlowRecord
from .base import BaseScraper, ScrapeContext
from .kmi import KMIScraper

log = logging.getLogger(__name__)


class TCEnergyScraper(BaseScraper):
    name = "tcenergy"

    def __init__(self, user_agent: str = "ng-feedgas/0.1 (+research)") -> None:
        super().__init__(user_agent)
        self._delegate = KMIScraper(user_agent)

    def fetch(self, ctx: ScrapeContext) -> list[FlowRecord]:
        return self._delegate.fetch(ctx)
