"""
Tests para Diagnóstico en Vivo, Matriz de Salud y Alertas de Silencio (Punto 1).
Verifica:
- Rastreo de escaneos vacíos consecutivos y disparo de alertas (anti-falsos positivos).
- Recuperación automática del estado de salud de la tienda.
- Ejecución limpia del comando CLI de diagnóstico.
"""

from unittest.mock import MagicMock, patch
import pytest

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.core.store_health import StoreHealthTracker
from glitchhunter_ai.run import run_diagnose


def test_store_health_consecutive_empty_alert_and_recovery():
    """
    Verifica que StoreHealthTracker detecte cuando una tienda devuelve 0 productos
    3 veces consecutivas, genere una única alerta sin inundar de spam,
    y restablezca el estado en cuanto la tienda se recupere.
    """
    tracker = StoreHealthTracker()
    store_id = "falabella"
    store_name = "Falabella Chile"

    # Escaneo 1: 0 productos
    tracker.record_scan(store_id, store_name, latency_ms=150.0, items_count=0)
    assert not tracker.should_alert_empty_store(store_id, threshold=3)

    # Escaneo 2: 0 productos
    tracker.record_scan(store_id, store_name, latency_ms=160.0, items_count=0)
    assert not tracker.should_alert_empty_store(store_id, threshold=3)

    # Escaneo 3: 0 productos -> Alerta debe activarse
    tracker.record_scan(store_id, store_name, latency_ms=140.0, items_count=0)
    assert tracker.should_alert_empty_store(store_id, threshold=3) is True

    # Escaneo 4: 0 productos -> No debe volver a disparar para evitar spam
    tracker.record_scan(store_id, store_name, latency_ms=145.0, items_count=0)
    assert tracker.should_alert_empty_store(store_id, threshold=3) is False

    # Escaneo 5: Tienda se recupera y devuelve 30 productos
    tracker.record_scan(store_id, store_name, latency_ms=210.0, items_count=30)
    matrix = tracker.get_matrix()
    rec = next(s for s in matrix if s["store_id"] == store_id)
    assert rec["status"] == "HEALTHY"
    assert rec["consecutive_empty"] == 0
    assert rec["empty_alert_dispatched"] is False


def test_pipeline_triggers_empty_store_health_alert(tmp_path):
    """
    Verifica que GlitchHuntingPipeline despache la alerta de salud
    cuando una tienda acumula escaneos vacíos en modo LIVE.
    """
    db = PriceDatabase(db_path=tmp_path / "test_health_alert.db")
    db.init_db()

    mock_scraper = MagicMock()
    mock_scraper.store_id = "test_store"
    mock_scraper.store_name = "Tienda Mock"
    mock_scraper.fetch_catalog.return_value = []

    pipeline = GlitchHuntingPipeline(db=db)
    pipeline.scrapers = [mock_scraper]

    with patch.object(pipeline, "_notify_store_health_alert") as mock_notify:
        # Ejecutar 3 escaneos vacíos en modo live
        pipeline.run_scan(use_live=True, send_notifications=False)
        pipeline.run_scan(use_live=True, send_notifications=False)
        pipeline.run_scan(use_live=True, send_notifications=False)

        assert mock_notify.call_count == 1
        mock_notify.assert_called_with("test_store", "Tienda Mock")


def test_run_diagnose_cli_with_mocked_scraper():
    """
    Verifica que la función del comando 'run.py diagnose' se ejecute
    e imprima la tabla de estado sin arrojar excepciones.
    """
    sample_product = ProductItem(
        id="sample-101",
        store_id="pcfactory",
        store_name="PC Factory",
        title="Ryzen 7 7800X3D",
        category="componentes",
        current_price=389990.0,
        normal_price=459990.0,
        url="https://pcfactory.cl/p/7800x3d",
        in_stock=True,
    )

    mock_scraper = MagicMock()
    mock_scraper.store_id = "pcfactory"
    mock_scraper.store_name = "PC Factory"
    mock_scraper.fetch_catalog.return_value = [sample_product]

    with patch("glitchhunter_ai.run.get_all_scrapers", return_value=[mock_scraper]):
        # No debe lanzar ninguna excepción
        run_diagnose(store_filter="pcfactory")
