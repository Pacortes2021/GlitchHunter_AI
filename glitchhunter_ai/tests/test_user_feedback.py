import os
import tempfile
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem, GlitchVerdict, GlitchAlert, GlitchSeverity, SemanticClassificationResult
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.notifications.telegram_bot import TelegramAlertDispatcher


@pytest.fixture
def temp_db():
    """Base de datos aislada temporal para pruebas de feedback."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = PriceDatabase(db_path=path)
    yield db
    if os.path.exists(path):
        os.remove(path)


def test_record_deal_feedback_and_counters(temp_db):
    """Prueba el registro de votos 👍/👎 y el incremento de contadores."""
    # Insertar producto y deal base
    product = ProductItem(
        id="prod-feedback-1",
        store_id="falabella",
        store_name="Falabella",
        title="Notebook Gamer Asus ROG Strix G16",
        category="notebooks",
        current_price=299990.0,
        normal_price=1299990.0,
        url="https://www.falabella.com/test",
    )
    temp_db.upsert_product(product)

    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=95.0,
        discount_percentage=76.9,
        absolute_savings_clp=1000000.0,
        semantic_check=SemanticClassificationResult(
            is_accessory=False,
            is_main_device=True,
            detected_item_type="notebook",
            explanation="Notebook gamer de gama alta",
            confidence=0.98,
        ),
        reason="Descuento crítico de 76.9%",
    )

    alert_id = "ALT-FAL-TEST1234"
    temp_db.register_or_update_deal(product, verdict, alert_id=alert_id)

    # 1. Voto positivo (👍)
    res_up = temp_db.record_deal_feedback(alert_id, "up")
    assert res_up["vote"] == "up"
    assert res_up["upvotes"] == 1
    assert res_up["downvotes"] == 0

    # 2. Voto negativo (👎)
    # Un segundo voto sobre la misma oferta reemplaza al anterior (no se acumula)
    res_down = temp_db.record_deal_feedback(alert_id, "down", reason="Precio inflado")
    assert res_down["vote"] == "down"
    assert res_down["upvotes"] == 0
    assert res_down["downvotes"] == 1

    # Clics repetidos no inflan el conteo
    temp_db.record_deal_feedback(alert_id, "down")
    res_again = temp_db.record_deal_feedback(alert_id, "down")
    assert res_again["downvotes"] == 1

    # 3. Voto inválido
    with pytest.raises(ValueError, match="Voto inválido"):
        temp_db.record_deal_feedback(alert_id, "maybe")


def test_get_feedback_stats(temp_db):
    """Verifica el cálculo de estadísticas agregadas de precisión del radar."""
    temp_db.record_deal_feedback("deal-1", "up", product_id="p1", store_id="falabella", category="notebooks")
    temp_db.record_deal_feedback("deal-2", "up", product_id="p2", store_id="falabella", category="notebooks")
    temp_db.record_deal_feedback("deal-3", "down", product_id="p3", store_id="ripley", category="smartphones")

    stats = temp_db.get_feedback_stats()
    assert stats["total_votes"] == 3
    assert stats["total_upvotes"] == 2
    assert stats["total_downvotes"] == 1
    # 2 / 3 = 66.7%
    assert stats["accuracy_rate_pct"] == 66.7
    assert "falabella" in stats["by_store"]
    assert stats["by_store"]["falabella"]["upvotes"] == 2
    assert stats["by_category"]["smartphones"]["downvotes"] == 1


def test_get_category_trust_modifier(temp_db):
    """Verifica que el modificador de confianza penalice falsos positivos y premie buenas ofertas."""
    # Caso 1: Menos del mínimo de muestras (3) -> 0.0
    temp_db.record_deal_feedback("d1", "down", store_id="paris", category="audio")
    temp_db.record_deal_feedback("d2", "down", store_id="paris", category="audio")
    assert temp_db.get_category_trust_modifier("paris", "audio", min_samples=3) == 0.0

    # Caso 2: 3 downvotes de 3 muestras (100% falsos positivos >= 75%) -> -15.0
    temp_db.record_deal_feedback("d3", "down", store_id="paris", category="audio")
    assert temp_db.get_category_trust_modifier("paris", "audio", min_samples=3) == -15.0

    # Caso 3: Categoría con alta tasa de éxito (>= 85% upvotes) -> +5.0
    for i in range(5):
        temp_db.record_deal_feedback(f"pc-{i}", "up", store_id="pcfactory", category="gpus")
    assert temp_db.get_category_trust_modifier("pcfactory", "gpus", min_samples=3) == 5.0


def test_pipeline_confidence_modulation_with_feedback(temp_db):
    """Verifica que el pipeline ajuste la certeza del veredicto basado en el historial de feedback."""
    pipeline = GlitchHuntingPipeline(db=temp_db)

    # Entrenar la BD con 3 reportes de falsos positivos en falabella/consolas
    for i in range(3):
        temp_db.record_deal_feedback(f"f-cons-{i}", "down", store_id="falabella", category="consolas")

    # Producto candidato a glitch en esa categoría
    product = ProductItem(
        id="prod-mod-1",
        store_id="falabella",
        store_name="Falabella",
        title="Consola PlayStation 5 825GB Digital Edition",
        category="consolas",
        current_price=99990.0,
        normal_price=499990.0,
        url="https://www.falabella.com/test-ps5",
    )

    alert = pipeline.process_product(product, send_notifications=False)
    assert alert is not None
    # El modificador de confianza debió penalizar con -15 puntos
    assert alert.verdict.user_trust_modifier == -15.0
    assert alert.verdict.confidence_score <= 85.0


def test_web_feedback_endpoints():
    """Verifica los endpoints FastAPI POST /api/deals/{deal_id}/feedback y GET /api/feedback/stats."""
    from glitchhunter_ai.web.server import app, db as server_db

    client = TestClient(app)
    server_db.upsert_product(ProductItem(
        id="feedback-deal-999", store_id="paris", store_name="Paris.cl", title="Smart TV LG 55",
        category="televisores", current_price=299990.0, normal_price=599990.0,
        url="https://www.paris.cl/feedback-deal-999.html", in_stock=True,
    ))

    # 1. Enviar voto positivo
    resp = client.post("/api/deals/feedback-deal-999/feedback", json={"vote": "up", "reason": "Excelente precio"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["deal_id"] == "feedback-deal-999"
    assert data["vote"] == "up"
    assert data["upvotes"] >= 1

    # 2. Enviar voto inválido
    resp_bad = client.post("/api/deals/feedback-deal-999/feedback", json={"vote": "invalido"})
    assert resp_bad.status_code == 400

    # 3. Oferta inexistente o motivo demasiado largo
    assert client.post("/api/deals/no-existe/feedback", json={"vote": "up"}).status_code == 404
    assert client.post("/api/deals/feedback-deal-999/feedback", json={"vote": "up", "reason": "x" * 501}).status_code == 422

    # 4. Voto desde otro sitio web (CSRF): rechazado
    foreign = client.post("/api/deals/feedback-deal-999/feedback", json={"vote": "down"}, headers={"Origin": "http://evil.example"})
    assert foreign.status_code == 403

    # 5. Consultar estadísticas de feedback
    resp_stats = client.get("/api/feedback/stats")
    assert resp_stats.status_code == 200
    stats = resp_stats.json()
    assert "total_votes" in stats
    assert "accuracy_rate_pct" in stats


def test_telegram_inline_keyboard_includes_feedback():
    """Verifica que el dispatcher de Telegram agregue botones de retroalimentación interactiva."""
    dispatcher = TelegramAlertDispatcher(simulation_mode=True)

    product = ProductItem(
        id="prod-tg-fb",
        store_id="ripley",
        store_name="Ripley",
        title="Smart TV OLED 55 LG C3",
        category="tvs",
        current_price=299990.0,
        normal_price=1199990.0,
        url="https://simple.ripley.cl/test-c3",
    )
    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=94.0,
        discount_percentage=75.0,
        absolute_savings_clp=900000.0,
        semantic_check=SemanticClassificationResult(
            is_accessory=False,
            is_main_device=True,
            detected_item_type="tv",
            explanation="Televisor OLED",
            confidence=0.99,
        ),
        reason="OLED a 75% OFF",
    )
    alert = GlitchAlert(
        alert_id="ALT-RIP-FB-TEST",
        product=product,
        verdict=verdict,
        alert_url=product.url,
        created_at=datetime.now(timezone.utc),
    )

    markup = dispatcher.build_inline_keyboard(alert)
    assert "inline_keyboard" in markup

    # Buscar botones de feedback por callback_data
    all_callbacks = [
        btn.get("callback_data")
        for row in markup["inline_keyboard"]
        for btn in row
        if "callback_data" in btn
    ]

    assert f"fb:up:{alert.alert_id}" in all_callbacks
    assert f"fb:down:{alert.alert_id}" in all_callbacks
