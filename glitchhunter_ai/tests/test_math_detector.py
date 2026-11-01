"""
Pruebas unitarias para el detector matemático de anomalías y Missing Zero Bug (MathAnomalyDetector).
"""

import pytest

from glitchhunter_ai.ai_detector.math_detector import MathAnomalyDetector
from glitchhunter_ai.core.models import GlitchSeverity, ProductItem


@pytest.fixture
def detector():
    return MathAnomalyDetector(
        min_discount_percentage=65.0,
        critical_discount_percentage=85.0,
        min_absolute_savings_clp=30000.0,
        missing_zero_ratio_min=8.5,
        missing_zero_ratio_max=11.5,
    )


def test_normal_product_no_discount(detector):
    """Producto a precio de lista sin descuento."""
    item = ProductItem(
        id="tv-01",
        store_id="falabella",
        store_name="Falabella",
        title="Smart TV Samsung 55",
        category="televisores",
        current_price=399990.0,
        normal_price=399990.0,
        url="https://falabella.com/tv",
    )
    res = detector.evaluate(item)

    assert res["is_glitch_candidate"] is False
    assert res["is_missing_zero"] is False
    assert res["discount_percentage"] == 0.0
    assert res["absolute_savings_clp"] == 0.0
    assert res["confidence_score"] == 0.0
    assert res["severity_hint"] == GlitchSeverity.FALSO_POSITIVO.value


def test_moderate_commercial_discount(detector):
    """Descuento comercial estándar (~30%) que no debe calificar como glitch."""
    item = ProductItem(
        id="audio-01",
        store_id="paris",
        store_name="Paris",
        title="Audífonos Bluetooth Sony",
        category="audio",
        current_price=34990.0,
        normal_price=49990.0,
        url="https://paris.cl/audifonos",
    )
    res = detector.evaluate(item)

    assert res["is_glitch_candidate"] is False
    assert res["is_missing_zero"] is False
    assert 29.0 <= res["discount_percentage"] <= 31.0
    assert res["absolute_savings_clp"] == 15000.0
    assert 10.0 <= res["confidence_score"] <= 35.0
    assert res["severity_hint"] == GlitchSeverity.FALSO_POSITIVO.value


def test_extreme_discount_glitch(detector):
    """Descuento masivo (88%) que supera umbrales pero no es missing zero."""
    item = ProductItem(
        id="laptop-01",
        store_id="ripley",
        store_name="Ripley",
        title="Notebook Gamer RTX 4080",
        category="notebooks",
        current_price=120000.0,
        normal_price=1000000.0,
        url="https://ripley.cl/laptop",
    )
    res = detector.evaluate(item)

    assert res["is_glitch_candidate"] is True
    assert res["is_missing_zero"] is False
    assert res["discount_percentage"] == 88.0
    assert res["absolute_savings_clp"] == 880000.0
    assert res["confidence_score"] >= 80.0
    assert res["severity_hint"] == GlitchSeverity.CRITICO.value


def test_missing_zero_bug_exact_pattern(detector):
    """Falta de cero clásica en retail chileno: $899.990 -> $89.990 (ratio 10.0x)."""
    item = ProductItem(
        id="phone-01",
        store_id="falabella",
        store_name="Falabella",
        title="Samsung Galaxy S24 Ultra",
        category="smartphones",
        current_price=89990.0,
        normal_price=899990.0,
        url="https://falabella.com/phone",
    )
    res = detector.evaluate(item)

    assert res["is_glitch_candidate"] is True
    assert res["is_missing_zero"] is True
    assert 9.9 <= res["missing_zero_ratio"] <= 10.1
    assert res["discount_percentage"] == 90.0
    assert res["absolute_savings_clp"] == 810000.0
    assert res["confidence_score"] >= 95.0
    assert res["severity_hint"] == GlitchSeverity.CRITICO.value


def test_missing_zero_with_card_price(detector):
    """Glitch de falta de un cero que se manifiesta en el precio con tarjeta de la tienda."""
    item = ProductItem(
        id="phone-cmr-01",
        store_id="falabella",
        store_name="Falabella",
        title="iPhone 15 Pro Max 256GB",
        category="smartphones",
        current_price=999990.0,
        normal_price=999990.0,
        card_price=99990.0,  # Con CMR cae un cero (ratio 10.0x)
        url="https://falabella.com/iphone",
    )
    res = detector.evaluate(item)

    assert res["is_glitch_candidate"] is True
    assert res["is_missing_zero"] is True
    assert res["effective_price"] == 99990.0
    assert res["discount_percentage"] == 90.0
    assert res["confidence_score"] >= 95.0


def test_baseline_price_evaluation(detector):
    """Evaluación exitosa contra baseline histórico cuando normal_price es 0 o no informado."""
    item = ProductItem(
        id="ps5-01",
        store_id="pcfactory",
        store_name="PC Factory",
        title="PlayStation 5 Slim Standard",
        category="consolas",
        current_price=54990.0,
        normal_price=0.0,
        url="https://pcfactory.cl/ps5",
    )
    res = detector.evaluate(item, baseline_price=549990.0)

    assert res["is_glitch_candidate"] is True
    assert res["is_missing_zero"] is True
    assert res["baseline_price"] == 549990.0
    assert res["discount_baseline"] == 90.0
    assert res["savings_baseline"] == 495000.0
    assert res["confidence_score"] >= 90.0


def test_baseline_contradiction_penalization(detector):
    """Cuando el precio normal fue inflado pero el baseline histórico demuestra que el descuento es falso."""
    item = ProductItem(
        id="bait-01",
        store_id="lider",
        store_name="Líder",
        title="Microondas 20L",
        category="electrohogar",
        current_price=50000.0,
        normal_price=200000.0,  # 75% inflado en lista
        url="https://lider.cl/microondas",
    )
    # Sin embargo, el histórico base era de $55.000 (~9% real)
    res = detector.evaluate(item, baseline_price=55000.0)

    assert res["details"]["baseline_contradiction"] is True
    # La confianza matemática debe ser penalizada severamente
    assert res["confidence_score"] < 60.0


def test_zero_or_negative_price_edge_case(detector):
    """Casos de borde: precios negativos o cero no deben causar ZeroDivisionError."""
    item = ProductItem(
        id="free-01",
        store_id="mercadolibre",
        store_name="Mercado Libre",
        title="Item con error de precio cero",
        category="varios",
        current_price=0.0,
        normal_price=100000.0,
        url="https://mercadolibre.cl/free",
    )
    res = detector.evaluate(item)

    assert res["is_glitch_candidate"] is True
    assert res["discount_percentage"] == 100.0
    assert res["absolute_savings_clp"] == 100000.0
    assert res["is_missing_zero"] is False  # current_price 0 no es división válida para ratio
