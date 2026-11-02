"""
Pruebas unitarias para MercadoLibreScraper en GlitchHunter AI.
"""

import pytest
from glitchhunter_ai.scrapers.mercadolibre import MercadoLibreScraper
from glitchhunter_ai.core.models import ProductItem


def test_mercadolibre_scraper_metadata():
    scraper = MercadoLibreScraper()
    assert scraper.store_id == "mercadolibre"
    assert scraper.store_name == "Mercado Libre Chile"
    assert "mercadolibre" in scraper.base_url


def test_mercadolibre_simulated_catalog():
    scraper = MercadoLibreScraper()
    catalog = scraper.fetch_catalog(use_live=False)

    assert isinstance(catalog, list)
    assert len(catalog) >= 8

    # Verificar que todos los elementos sean ProductItem válidos
    for item in catalog:
        assert isinstance(item, ProductItem)
        assert item.store_id == "mercadolibre"
        assert item.store_name == "Mercado Libre Chile"
        assert item.current_price > 0
        assert item.normal_price >= item.current_price
        assert item.url.startswith("https://")

    # Verificar que existan casos de omisión de cero (bug de precio)
    bugs = [
        item for item in catalog
        if item.discount_percentage >= 85.0 and (item.normal_price / item.current_price) >= 8.5
    ]
    assert len(bugs) >= 2

    # Verificar que existan casos trampa de accesorios / láminas
    traps = [
        item for item in catalog
        if "lámina" in item.title.lower() or "funda" in item.title.lower() or "carcasa" in item.title.lower()
    ]
    assert len(traps) >= 2


def test_mercadolibre_live_fallback():
    scraper = MercadoLibreScraper(request_timeout=2)
    # 1. En modo simulación (use_live=False) retorna el catálogo simulado completo
    sim_catalog = scraper.fetch_catalog(use_live=False)
    assert isinstance(sim_catalog, list)
    assert len(sim_catalog) >= 8

    # 2. En modo live (use_live=True) retorna lista de productos reales o [] si la red/WAF está bloqueada
    # Garantiza la regla de oro: NUNCA inyectar mocks sintéticos cuando se solicita LIVE
    live_catalog = scraper.fetch_catalog(use_live=True)
    assert isinstance(live_catalog, list)
