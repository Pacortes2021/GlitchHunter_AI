"""
Scrapers de retail para GlitchHunter AI.
Incluye los 6 principales retailers chilenos:
- Falabella
- Ripley
- Paris (Cencosud)
- Líder (Walmart Chile)
- Mercado Libre Chile
- PC Factory
"""

from typing import List, Any
from glitchhunter_ai.scrapers.falabella import FalabellaScraper
from glitchhunter_ai.scrapers.ripley import RipleyScraper
from glitchhunter_ai.scrapers.paris import ParisScraper
from glitchhunter_ai.scrapers.lider import LiderScraper
from glitchhunter_ai.scrapers.mercadolibre import MercadoLibreScraper
from glitchhunter_ai.scrapers.pcfactory import PCFactoryScraper

__all__ = [
    "FalabellaScraper",
    "RipleyScraper",
    "ParisScraper",
    "LiderScraper",
    "MercadoLibreScraper",
    "PCFactoryScraper",
    "get_all_scrapers",
]


def get_all_scrapers() -> List[Any]:
    """Retorna instancias inicializadas de los 6 scrapers de retail."""
    return [
        FalabellaScraper(),
        RipleyScraper(),
        ParisScraper(),
        LiderScraper(),
        MercadoLibreScraper(),
        PCFactoryScraper(),
    ]
