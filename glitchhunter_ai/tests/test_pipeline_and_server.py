"""
Pruebas de integración para el pipeline orquestador y el servidor FastAPI de GlitchHunter AI.
"""

import pytest
from fastapi.testclient import TestClient

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import GlitchSeverity, ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.web.server import app


@pytest.fixture
def clean_db(tmp_path):
    test_db_path = tmp_path / "test_glitchhunter.db"
    db = PriceDatabase(db_path=test_db_path)
    db.init_db()
    return db


def test_pipeline_scan_and_accessory_filter(clean_db):
    pipeline = GlitchHuntingPipeline(db=clean_db)
    glitches = pipeline.run_scan(use_live=False)

    assert len(glitches) > 0, "Debe detectar al menos los glitches sembrados en los catálogos simulados"

    # Verificar que cada glitch sea legítimo y no una trampa de accesorio
    for g in glitches:
        assert g.verdict.is_glitch is True
        assert g.verdict.semantic_check.is_accessory is False or g.verdict.semantic_check.is_main_device is True
        assert g.alert_url is not None
        assert g.product.store_id in ["falabella", "ripley", "paris", "lider", "mercadolibre", "pcfactory", "solotodo"]

    # Verificar que el missing zero bug esté presente en los productos clave
    missing_zero_glitches = [g for g in glitches if g.verdict.is_missing_zero]
    assert len(missing_zero_glitches) >= 5, "Debe detectar los casos de falta de un cero (~10x)"


def test_server_status_and_stats_endpoints():
    with TestClient(app) as client:
        res = client.get("/api/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "online"
        assert data["stores_monitored"] >= 6

        res_stats = client.get("/api/stats")
        assert res_stats.status_code == 200
        stats = res_stats.json()
        assert "total_products" in stats
        assert "accumulated_savings_clp" in stats


def test_server_glitches_query_and_filtering():
    with TestClient(app) as client:
        # Consulta sin filtro
        res = client.get("/api/glitches?limit=10")
        assert res.status_code == 200
        items = res.json()
        assert isinstance(items, list)

        if items:
            first = items[0]
            assert "product" in first
            assert "verdict" in first
            assert "alert_id" in first
            assert "alert_url" in first

        # Filtro por tienda
        res_falabella = client.get("/api/glitches?store_id=falabella")
        assert res_falabella.status_code == 200
        fal_items = res_falabella.json()
        for item in fal_items:
            assert item["product"]["store_id"].lower() == "falabella"


def test_server_commercial_deals_endpoint():
    with TestClient(app) as client:
        res = client.get("/api/deals?min_discount=40&max_discount=65&limit=20")
        assert res.status_code == 200
        items = res.json()
        assert isinstance(items, list)
        for d in items:
            assert "product" in d
            assert "verdict" in d
            assert 40.0 <= d["verdict"]["discount_percentage"] <= 65.0


def test_server_inject_test_glitch():
    with TestClient(app) as client:
        res = client.post("/api/test-glitch")
        assert res.status_code == 200
        data = res.json()
        assert "alert_id" in data
        assert "product" in data
        assert "verdict" in data
        assert data["verdict"]["is_glitch"] is True


def test_server_websocket_connection():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/glitches") as websocket:
            welcome = websocket.receive_json()
            assert welcome["type"] == "WELCOME"
            websocket.send_text("ping")
            pong = websocket.receive_text()
            assert pong == "pong"
