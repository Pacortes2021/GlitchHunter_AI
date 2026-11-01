"""
Pruebas unitarias para los modelos Pydantic v2 de GlitchHunter AI.
"""

from datetime import datetime
import pytest

from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    PriceHistoryRecord,
    ProductItem,
    SemanticClassificationResult,
    TelegramAlertPayload,
)


def test_glitch_severity_enum():
    assert GlitchSeverity.CRITICO == "CRITICO"
    assert GlitchSeverity.ALTO == "ALTO"
    assert GlitchSeverity.MODERADO == "MODERADO"
    assert GlitchSeverity.FALSO_POSITIVO == "FALSO_POSITIVO"


def test_product_item_properties():
    item = ProductItem(
        id="fal-12345",
        store_id="falabella",
        store_name="Falabella",
        title="Consola PlayStation 5",
        category="consolas",
        current_price=100000.0,
        normal_price=500000.0,
        card_price=80000.0,
        url="https://falabella.com/ps5",
        image_url="https://falabella.com/ps5.jpg",
        in_stock=True,
    )
    assert item.best_price == 80000.0
    assert item.discount_percentage == 84.0
    assert item.absolute_savings_clp == 420000.0


def test_product_item_without_card_price():
    item = ProductItem(
        id="rip-999",
        store_id="ripley",
        store_name="Ripley",
        title="Notebook Gamer RTX 4060",
        category="notebooks",
        current_price=200000.0,
        normal_price=1000000.0,
        url="https://ripley.cl/laptop",
    )
    assert item.best_price == 200000.0
    assert item.discount_percentage == 80.0
    assert item.absolute_savings_clp == 800000.0


def test_price_history_record():
    record = PriceHistoryRecord(
        product_id="paris-777",
        price=150000.0,
        store_id="paris",
    )
    assert record.product_id == "paris-777"
    assert record.price == 150000.0
    assert isinstance(record.recorded_at, datetime)


def test_semantic_classification():
    sem = SemanticClassificationResult(
        is_accessory=True,
        is_main_device=False,
        detected_item_type="carcasa_funda",
        explanation="Carcasa de silicona para iPhone 15",
        confidence=0.95,
    )
    assert sem.is_accessory is True
    assert sem.is_main_device is False
    assert sem.confidence == 0.95


def test_glitch_verdict_and_alert():
    item = ProductItem(
        id="lead-01",
        store_id="lider",
        store_name="Líder",
        title="Smart TV OLED 65 pulgadas",
        category="televisores",
        current_price=59990.0,
        normal_price=899990.0,
        url="https://lider.cl/tv",
    )
    sem = SemanticClassificationResult(
        is_accessory=False,
        is_main_device=True,
        detected_item_type="televisor",
        explanation="Pantalla principal 65 pulgadas",
        confidence=0.99,
    )
    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=97.0,
        discount_percentage=item.discount_percentage,
        absolute_savings_clp=item.absolute_savings_clp,
        is_missing_zero=True,
        is_fake_inflation=False,
        semantic_check=sem,
        reason="Caída de un dígito en TV de alta gama",
    )
    alert = GlitchAlert(
        product=item,
        verdict=verdict,
        alert_url=item.url,
    )
    assert alert.alert_id.startswith("ALT-")
    assert alert.verdict.is_glitch is True
    assert alert.verdict.severity == GlitchSeverity.CRITICO

    # Serialization roundtrip test
    data = alert.model_dump()
    assert data["product"]["id"] == "lead-01"
    restored = GlitchAlert.model_validate(data)
    assert restored.alert_id == alert.alert_id


def test_telegram_alert_payload():
    tg = TelegramAlertPayload(
        chat_id="@test_channel",
        text="<b>Alerta de Test</b>",
    )
    assert tg.chat_id == "@test_channel"
    assert tg.parse_mode == "HTML"
    assert tg.reply_markup is None
