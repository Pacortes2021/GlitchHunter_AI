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

    # Debe incluir 'ordering=offer_price_usd' y 'categories=2' en SoloTodo
    solotodo_call = next(u for u in called_urls if "solotodo.com" in u)
    assert "ordering=offer_price_usd" in solotodo_call
    assert "categories=2" in solotodo_call

    # Debe incluir 'order=price_asc' en Knasta
    knasta_call = next(u for u in called_urls if "knasta.cl" in u)
    assert "order=price_asc" in knasta_call


def test_live_feed_search_live_products_parsing():
    """Verifica que search_live_products busque y estructure productos de múltiples tiendas."""
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "products": [
            {
                "product_id": "111",
                "retail": "falabella",
                "title": "Consola PlayStation 5 Slim 1TB",
                "price_value": "49990",
                "price_tm": "549990",
                "url": "https://falabella.com/p/ps5",
                "images": "https://falabella.com/ps5.jpg",
            },
            {
                "product_id": "222",
                "retail": "pcfactory",
                "title": "Control DualSense PS5 Blanco",
                "price_value": "69990",
                "price_tm": "79990",
                "url": "https://pcfactory.cl/p/dualsense",
                "images": "https://pcfactory.cl/ctrl.jpg",
            },
        ]
    }
    mock_session.get.return_value = mock_resp

    feed = LiveRetailFeed(timeout=3.0)
    feed.session = mock_session

    results = feed.search_live_products(query="playstation 5", order="price_asc", limit=10)
    assert len(results) == 2
    assert results[0].store_id == "falabella"
    assert results[0].current_price == 49990.0
    assert results[0].normal_price == 549990.0
    assert results[1].store_id == "pcfactory"


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
