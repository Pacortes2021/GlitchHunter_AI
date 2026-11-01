"""
Pruebas unitarias para el verificador de inflación artificial de precios (InflationVerifier).
"""

from datetime import datetime, timedelta
import pytest

from glitchhunter_ai.ai_detector.inflation_verifier import InflationVerifier
from glitchhunter_ai.core.models import PriceHistoryRecord, ProductItem


@pytest.fixture
def verifier():
    return InflationVerifier(inflation_threshold=1.4, min_glitch_discount=65.0)


def test_verify_fake_inflation_deal(verifier):
    # Zapatilla normalmente a $50.000, inflada a $150.000 y rebajada a $45.000 (10% real)
    item = ProductItem(
        id="zap-01",
        store_id="falabella",
        store_name="Falabella",
        title="Zapatillas Running",
        category="calzado",
        current_price=45000.0,
        normal_price=150000.0,
        url="https://falabella.com/zap",
    )
    is_fake, real_discount, explanation = verifier.verify_inflation(item, baseline_price=50000.0)

    assert is_fake is True
    assert real_discount == 10.0
    assert "FALSA INFLACIÓN / FAKE DEAL DESCARTADO" in explanation
    assert verifier.is_glitch_candidate(item, baseline_price=50000.0) is False


def test_verify_genuine_glitch(verifier):
    # TV OLED de $899.990 publicado a $89.990 (glitch del 90%)
    item = ProductItem(
        id="tv-01",
        store_id="ripley",
        store_name="Ripley",
        title="Smart TV OLED 65",
        category="televisores",
        current_price=89990.0,
        normal_price=899990.0,
        url="https://ripley.cl/tv",
    )
    is_fake, real_discount, explanation = verifier.verify_inflation(item, baseline_price=899990.0)

    assert is_fake is False
    assert real_discount == 90.0
    assert "GLITCH GENUINO CONFIRMADO" in explanation
    assert verifier.is_glitch_candidate(item, baseline_price=899990.0) is True


def test_verify_inflated_but_genuine_extreme_glitch(verifier):
    # Precio inflado a $2.000.000 (base $1.200.000, +66%), pero precio de liquidación a $100.000 (descuento real > 90%)
    item = ProductItem(
        id="pc-01",
        store_id="pcfactory",
        store_name="PC Factory",
        title="PC Gamer Intel i9",
        category="computacion",
        current_price=100000.0,
        normal_price=2000000.0,
        url="https://pcfactory.cl/pc",
    )
    is_fake, real_discount, explanation = verifier.verify_inflation(item, baseline_price=1200000.0)

    assert is_fake is True
    assert real_discount > 91.0
    assert "GLITCH REAL CONFIRMADO" in explanation
    assert verifier.is_glitch_candidate(item, baseline_price=1200000.0) is True


def test_verify_without_baseline(verifier):
    item = ProductItem(
        id="item-01",
        store_id="lider",
        store_name="Líder",
        title="Aspiradora Robot",
        category="hogar",
        current_price=50000.0,
        normal_price=200000.0,
        url="https://lider.cl/aspiradora",
    )
    is_fake, real_discount, explanation = verifier.verify_inflation(item, baseline_price=None)

    assert is_fake is False
    assert real_discount == 75.0
    assert "Sin Historial Base" in explanation


def test_compute_baseline_from_records(verifier):
    records = [
        PriceHistoryRecord(product_id="prod-1", price=50000.0, store_id="paris", recorded_at=datetime.utcnow() - timedelta(days=2)),
        PriceHistoryRecord(product_id="prod-1", price=52000.0, store_id="paris", recorded_at=datetime.utcnow() - timedelta(days=10)),
        PriceHistoryRecord(product_id="prod-1", price=48000.0, store_id="paris", recorded_at=datetime.utcnow() - timedelta(days=20)),
    ]
    baseline = verifier.compute_baseline_from_records(records, max_days=30)
    assert baseline == 50000.0


def test_invalid_product_normal_price(verifier):
    item = ProductItem(
        id="err-01",
        store_id="falabella",
        store_name="Falabella",
        title="Item Error",
        category="otros",
        current_price=1000.0,
        normal_price=0.0,
        url="https://falabella.com/err",
    )
    is_fake, real_discount, explanation = verifier.verify_inflation(item, baseline_price=10000.0)
    assert is_fake is False
    assert real_discount == 0.0
    assert "Error" in explanation
