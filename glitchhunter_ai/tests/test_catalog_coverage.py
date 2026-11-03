"""
Tests unitarios y de integración para la Ampliación de Cobertura de Catálogo (Punto 3).
Cubre:
1. Rotación Round-Robin de categorías y alternancia de estrategias de ordenamiento (CatalogRotator).
2. Consultas y filtrado en LiveRetailFeed con sort_strategy ('price_asc' vs 'discount_desc') y búsqueda proactiva.
3. Escaneo dedicado y prioritario de Watchlist en el pipeline.
4. Integración de rotación y watchlist en run_scan() y endpoints web FastAPI.
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from glitchhunter_ai.core.catalog_rotator import (
    CatalogRotator,
    CategoryDefinition,
    SortStrategy,
)
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
from glitchhunter_ai.web.server import app


# =====================================================================
# 1. Pruebas de Rotación Round-Robin de Categorías y Estrategias
# =====================================================================
def test_catalog_rotator_round_robin_and_strategy_alternation():
    """Verifica el ciclado equitativo de categorías y la alternancia de ordenamiento."""
    dummy_cats = [
        CategoryDefinition(key=f"cat_{i}", display_name=f"Categoría {i}", search_terms=[f"term_{i}"])
        for i in range(5)
    ]
    rotator = CatalogRotator(categories=dummy_cats, batch_size=2)

    # Ciclo 1: debe tomar [cat_0, cat_1] con DISCOUNT_DESC
    batch1 = rotator.get_next_batch()
    assert batch1.cycle_number == 1
    assert len(batch1.categories) == 2
    assert [c.key for c in batch1.categories] == ["cat_0", "cat_1"]
    assert batch1.strategy == SortStrategy.DISCOUNT_DESC

    # Ciclo 2: debe tomar [cat_2, cat_3] con PRICE_ASC
    batch2 = rotator.get_next_batch()
    assert batch2.cycle_number == 2
    assert [c.key for c in batch2.categories] == ["cat_2", "cat_3"]
    assert batch2.strategy == SortStrategy.PRICE_ASC

    # Ciclo 3: debe envolver al inicio [cat_4, cat_0] con DISCOUNT_DESC
    batch3 = rotator.get_next_batch()
    assert batch3.cycle_number == 3
    assert [c.key for c in batch3.categories] == ["cat_4", "cat_0"]
    assert batch3.strategy == SortStrategy.DISCOUNT_DESC

    # Diagnóstico de estado
    status = rotator.get_status()
    assert status["cycle_counter"] == 3
    assert status["total_categories"] == 5

    # Reset
    rotator.reset()
    assert rotator.get_status()["cycle_counter"] == 0
    assert rotator.get_status()["current_cursor_index"] == 0


# =====================================================================
# 2. Pruebas de LiveRetailFeed con Ordenamiento y Búsqueda Proactiva
# =====================================================================
def _knasta_html(products):
    """Página https://knasta.cl/results con la estructura real de su __NEXT_DATA__."""
    import json as _json
    data = {"props": {"pageProps": {"initialData": {"count": len(products), "products": products}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{_json.dumps(data)}</script></html>'


def _mock_session(knasta_products=None):
    mock_session = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"results": []}
    mock_resp.text = _knasta_html(knasta_products or [])
    mock_session.get.return_value = mock_resp
    return mock_session


def test_live_feed_sort_strategy_urls():
    """Verifica que LiveRetailFeed configure correctamente los parámetros de ordenamiento."""
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": [], "products": []}
    mock_session.get.return_value = mock_resp

    feed = LiveRetailFeed(timeout=3.0)
    feed.session = mock_session

    # Llamada con price_asc
    feed.fetch_live_products_for_store("falabella", limit=20, sort_strategy="price_asc", category_id=2)
    called_urls = [call[0][0] for call in mock_session.get.call_args_list]

    # SoloTodo /entities/ ignora 'offer_price_usd' y '-discount_percentage': se usa '-id' (efectivo)
    # junto al filtro por categoría, que es lo que realmente acota el catálogo
    solotodo_call = next(u for u in called_urls if "solotodo.com" in u)
    assert "ordering=-id" in solotodo_call
    assert "categories=2" in solotodo_call

    # Knasta: página pública /results (robots.txt prohíbe /api), bajadas de hoy de esta tienda
    knasta_call = next(u for u in called_urls if "knasta.cl" in u)
    assert "/api/" not in knasta_call
    assert "knastaday=1" in knasta_call and "partners=falabella" in knasta_call


def test_live_feed_search_live_products_parsing():
    """Verifica que search_live_products busque y estructure productos de múltiples tiendas."""
    mock_session = _mock_session([
        {
            "product_id": "111",
            "retail": "falabella",
            "title": "Consola PlayStation 5 Slim 1TB",
            "current_price": 49990,
            "url": "https://falabella.com/p/ps5",
            "image": "https://falabella.com/ps5.jpg",
            "descriptive": {"state": "OPPORTUNITY", "habitual_price": 549990},
        },
        {
            "product_id": "222",
            "retail": "pcfactory",
            "title": "Control DualSense PS5 Blanco",
            "current_price": 69990,
            "last_variation_price": 79990,
            "url": "https://pcfactory.cl/p/dualsense",
        },
    ])

    feed = LiveRetailFeed(timeout=3.0)
    feed.session = mock_session

    results = feed.search_live_products(query="playstation 5", order="price_asc", limit=10)
    assert len(results) == 2
    assert results[0].store_id == "falabella"
    assert results[0].current_price == 49990.0
    assert results[0].normal_price == 549990.0   # precio habitual de Knasta
    assert results[1].store_id == "pcfactory"
    assert results[1].normal_price == 79990.0    # sin historial: último precio distinto
    assert "knasta.cl/results?q=" in mock_session.get.call_args_list[0][0][0]


# =====================================================================
# 3. Pruebas de Escaneo Dedicado de Watchlist en Pipeline
# =====================================================================
def test_pipeline_watchlist_scan_and_alert_generation(tmp_path):
    """Verifica que el pipeline escanee proactivamente las palabras clave de la watchlist."""
    db = PriceDatabase(db_path=tmp_path / "test_wl_scan.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    # 1. Sin palabras clave: debe retornar lista vacía limpiamente
    alerts_empty = pipeline.run_watchlist_scan(use_live=False, send_notifications=False)
    assert alerts_empty == []

    # 2. Agregar palabra clave a la watchlist
    db.add_watchlist_keyword("rtx 4080")
    assert db.get_watchlist_keywords() == ["rtx 4080"]

    # 3. Escanear watchlist (modo simulación / test)
    alerts = pipeline.run_watchlist_scan(use_live=False, send_notifications=False)
    assert len(alerts) >= 1
    alert = alerts[0]
    assert "Rtx 4080" in alert.product.title or "rtx 4080" in alert.product.title.lower()
    assert alert.verdict.is_glitch is True
    assert alert.is_watchlist_match is True


def test_pipeline_run_scan_includes_rotation_and_watchlist(tmp_path):
    """Verifica que run_scan ejecute el ciclo con rotación y recolecte alertas de watchlist."""
    db = PriceDatabase(db_path=tmp_path / "test_full_scan.db")
    db.init_db()
    db.add_watchlist_keyword("macbook pro")

    pipeline = GlitchHuntingPipeline(db=db)

    # Mock de scrapers para ejecución rápida y predecible
    mock_scraper = MagicMock()
    mock_scraper.store_id = "test_store"
    mock_scraper.store_name = "Test Retail"
    mock_scraper.fetch_catalog.return_value = []
    pipeline.scrapers = [mock_scraper]

    glitches = pipeline.run_scan(
        use_live=False,
        send_notifications=False,
        rotate_categories=True,
        scan_watchlist=True,
    )

    # Debe haber ejecutado la rotación
    assert pipeline.catalog_rotator.get_status()["cycle_counter"] >= 1
    # Debe haber detectado el glitch de la watchlist
    assert len(glitches) >= 1
    assert any("Macbook Pro" in g.product.title for g in glitches)


# =====================================================================
# 4. Pruebas de Endpoints Web FastAPI (Watchlist Scan y Rotación)
# =====================================================================
def test_web_endpoints_watchlist_scan_and_catalog_rotation():
    """Verifica las rutas /api/watchlist/scan y /api/catalog/rotation."""
    with TestClient(app) as client:
        # 1. Consultar rotación
        res_rot = client.get("/api/catalog/rotation")
        assert res_rot.status_code == 200
        rot_data = res_rot.json()
        assert "total_categories" in rot_data
        assert "current_strategy" in rot_data

        # 2. Agregar palabra clave a watchlist
        res_add = client.post("/api/watchlist", json={"keyword": "ipad air"})
        assert res_add.status_code == 200

        # 3. Disparar escaneo de watchlist
        with patch.object(app.state if hasattr(app, "state") else app, "pipeline", None, create=True):
            pass
        from glitchhunter_ai.web.server import pipeline as server_pipeline
        with patch.object(server_pipeline, "run_watchlist_scan", return_value=[]):
            res_wl_scan = client.post("/api/watchlist/scan")
            assert res_wl_scan.status_code == 200
            wl_data = res_wl_scan.json()
            assert wl_data["status"] == "success"
            assert "ipad air" in wl_data["scanned_keywords"]

            # 4. Disparar escaneo manual con watchlist_only=true
            with patch.object(server_pipeline, "is_scanning", False):
                res_scan_wl = client.post("/api/scan?watchlist_only=true")
                assert res_scan_wl.status_code == 200
                assert res_scan_wl.json()["status"] == "success"



# =====================================================================
# Cobertura por categoría en todas las tiendas
# =====================================================================
def test_rotator_solotodo_category_ids_match_category_meaning():
    """IDs verificados contra https://publicapi.solotodo.com/categories/ (sin duplicados entre categorías)."""
    from glitchhunter_ai.core.catalog_rotator import DEFAULT_CATEGORIES

    ids = {c.key: c.solotodo_category_ids for c in DEFAULT_CATEGORIES}
    assert ids["smartwatch"] == [48]              # Wearables (30 era "Tarjetas de memoria")
    assert ids["linea_blanca"] == [15, 19, 17]    # Refrigeradores, Lavadoras, Hornos (37 era "All-in-One")
    assert ids["gaming"] == [40, 41, 54]          # Mouse, Teclados, Sillas Gamer (33 repetía Consolas)
    assert ids["componentes"] == [2, 3]           # Tarjetas de video + Procesadores
    flat = [i for v in ids.values() for i in v]
    assert len(flat) == len(set(flat)), "Una categoría de SoloTodo no debe repetirse en dos categorías del radar"


def test_live_feed_sends_all_cycle_categories_to_solotodo():
    mock_session = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"results": [], "products": []}
    mock_session.get.return_value = mock_resp

    feed = LiveRetailFeed(timeout=3.0)
    feed.session = mock_session
    feed.fetch_live_products_for_store("paris", category_ids=[15, 19, 17])

    solotodo_call = next(c[0][0] for c in mock_session.get.call_args_list if "solotodo.com" in c[0][0])
    assert "stores=11" in solotodo_call
    for cid in (15, 19, 17):
        assert f"categories={cid}" in solotodo_call


def test_store_scrapers_receive_rotation_categories():
    """
    Antes solo el scraper de SoloTodo recibía las categorías del ciclo: Falabella, Ripley, Paris,
    Líder y PC Factory pedían siempre el mismo listado (las entidades más antiguas, ej: cartuchos de tinta).
    """
    from glitchhunter_ai.core.database import PriceDatabase
    from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
    from glitchhunter_ai.core.catalog_rotator import CatalogRotator

    import tempfile, os
    db = PriceDatabase(db_path=os.path.join(tempfile.mkdtemp(), "rot.db"))
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db)
    pipeline.catalog_rotator = CatalogRotator(batch_size=1)  # primer ciclo: smartphones (SoloTodo 6)

    from glitchhunter_ai.scrapers.falabella import FalabellaScraper
    from glitchhunter_ai.scrapers.ripley import RipleyScraper
    from glitchhunter_ai.scrapers.paris import ParisScraper
    from glitchhunter_ai.scrapers.lider import LiderScraper
    from glitchhunter_ai.scrapers.pcfactory import PCFactoryScraper

    # Sin red: el agregador y la lectura propia de cada tienda (respaldo) devuelven vacío
    with patch.object(LiveRetailFeed, "fetch_live_products_for_store", return_value=[]) as feed_call, \
            patch.object(FalabellaScraper, "fetch_live_catalog", return_value=[]), \
            patch.object(RipleyScraper, "_fetch_live", return_value=[]), \
            patch.object(ParisScraper, "_fetch_live_catalog", return_value=[]), \
            patch.object(LiderScraper, "fetch_live_catalog", return_value=[]), \
            patch.object(PCFactoryScraper, "fetch_live_catalog", return_value=[]):
        pipeline.run_scan(
            use_live=True,
            store_ids=["falabella", "ripley", "paris", "lider", "pcfactory"],
            send_notifications=False,
            scan_watchlist=False,
        )

    stores_with_categories = {
        c.args[0] for c in feed_call.call_args_list if c.kwargs.get("category_ids") == [6]
    }
    assert stores_with_categories == {"falabella", "ripley", "paris", "lider", "pcfactory"}


def test_knasta_normal_price_uses_previous_price():
    """Knasta calcula su descuento contra 'prev_price'; 'price_tm' suele ser igual al precio actual."""
    from glitchhunter_ai.scrapers.live_feed import knasta_normal_price

    sandwichera = {"price_value": 12990.0, "price_tm": 12990.0, "prev_price": "$ 24.990", "percent": -48}
    assert knasta_normal_price(sandwichera, 12990.0) == 24990.0
    # Con análisis de historial, manda el precio habitual (moda) aunque el anterior sea mayor
    con_historial = {"last_variation_price": 1499990, "descriptive": {"habitual_price": 1229990}}
    assert knasta_normal_price(con_historial, 739990.0) == 1229990.0
    # Si no hay precio anterior, se usa price_tm o, en último caso, el precio actual
    assert knasta_normal_price({"price_tm": 14990.0}, 11990.0) == 14990.0
    assert knasta_normal_price({}, 11990.0) == 11990.0


def test_knasta_pages_are_cached_between_scrapers():
    """Una misma página de Knasta no se descarga dos veces dentro del TTL (ej: scraper + escaneo de watchlist)."""
    from glitchhunter_ai.scrapers import live_feed

    live_feed._knasta_cache.clear()
    mock_session = _mock_session([])
    feed = LiveRetailFeed(timeout=3.0)
    feed.session = mock_session
    feed.fetch_live_products_for_store("paris")
    feed.fetch_live_products_for_store("paris")

    knasta_calls = [c for c in mock_session.get.call_args_list if "knasta.cl" in c[0][0]]
    assert len(knasta_calls) == live_feed.KNASTA_PAGES_PER_STORE
    live_feed._knasta_cache.clear()


def test_knasta_keeps_only_real_deals_by_price_history():
    """
    Knasta analiza el historial de cada producto: solo 'OPPORTUNITY' y 'BELOW_HABITUAL' son ofertas reales.
    El precio de referencia es el habitual (moda del historial), no el precio tachado.
    """
    from glitchhunter_ai.scrapers import live_feed

    live_feed._knasta_cache.clear()
    base = {"retail": "lider", "url": "https://www.lider.cl/ip/{}", "title": "Producto {}", "current_price": 739990}
    items = []
    for n, state in enumerate(["OPPORTUNITY", "BELOW_HABITUAL", "HABITUAL", "ABOVE_HABITUAL", None]):
        it = dict(base, product_id=str(n), url=base["url"].format(n), title=base["title"].format(n))
        it["descriptive"] = {"state": state, "habitual_price": 1229990} if state else None
        items.append(it)
    items.append(dict(items[0], product_id="99", retail="paris", url="https://www.paris.cl/x-99.html"))

    feed = LiveRetailFeed(timeout=3.0)
    feed.session = _mock_session(items)
    products = [p for p in feed.fetch_live_products_for_store("lider") if p.source == "knasta"]

    assert sorted(p.title for p in products) == ["Producto 0", "Producto 1"]
    assert all(p.normal_price == 1229990 and p.store_id == "lider" for p in products)
    live_feed._knasta_cache.clear()


def test_knasta_not_queried_for_ripley():
    """Knasta no cubre Ripley ('partners=ripley' devuelve todas las tiendas): no se consulta."""
    from glitchhunter_ai.scrapers import live_feed

    live_feed._knasta_cache.clear()
    mock_session = _mock_session([])
    feed = LiveRetailFeed(timeout=3.0)
    feed.session = mock_session
    feed.fetch_live_products_for_store("ripley")
    assert not [c for c in mock_session.get.call_args_list if "knasta.cl" in c[0][0]]
