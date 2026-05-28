"""Scrapers for pipeline EBB portals."""
from .base import BaseScraper, ScrapeContext
from .kmi import KMIScraper
from .williams import WilliamsScraper
from .williams_nwp import WilliamsNWPScraper
from .tcenergy import TCEnergyScraper
from .enbridge import EnbridgeTETCOScraper
from .energytransfer import EnergyTransferTGCScraper
from .et_ipost import EnergyTransferIPostScraper
from .tceconnects import TCeConnectsScraper
from .iroquois import IroquoisScraper

SCRAPERS: dict[str, type[BaseScraper]] = {
    "kmi": KMIScraper,
    "williams": WilliamsScraper,
    "williams_nwp": WilliamsNWPScraper,
    "tcenergy": TCEnergyScraper,
    "enbridge": EnbridgeTETCOScraper,
    "et_tgc": EnergyTransferTGCScraper,
    "et_ipost": EnergyTransferIPostScraper,
    "tceconnects": TCeConnectsScraper,
    "iroquois": IroquoisScraper,
}

__all__ = [
    "BaseScraper", "ScrapeContext",
    "KMIScraper", "WilliamsScraper", "WilliamsNWPScraper",
    "TCEnergyScraper",
    "EnbridgeTETCOScraper",
    "EnergyTransferTGCScraper", "EnergyTransferIPostScraper",
    "TCeConnectsScraper",
    "IroquoisScraper",
    "SCRAPERS",
]
