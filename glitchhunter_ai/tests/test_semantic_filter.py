"""
Pruebas unitarias para el filtro semántico anti-carcasas y accesorios (SemanticGlitchFilter).
"""

import pytest

from glitchhunter_ai.ai_detector.semantic_filter import SemanticGlitchFilter
from glitchhunter_ai.core.models import SemanticClassificationResult


@pytest.fixture
def filter_engine():
    return SemanticGlitchFilter()


def test_main_device_iphone_glitch(filter_engine):
    """Verifica que un iPhone con especificaciones sea clasificado como equipo principal y glitch confirmado."""
    title = "iPhone 15 Pro Max 256GB Titanio Natural"
    eval_res = filter_engine.evaluate_glitch_candidate(
        title=title,
        current_price=119990.0,
        normal_price=1299990.0,
    )
    sem = eval_res["semantic_result"]
    assert isinstance(sem, SemanticClassificationResult)
    assert sem.is_main_device is True
    assert sem.is_accessory is False
    assert sem.detected_item_type == "smartphone"
    assert sem.confidence >= 0.90
    assert eval_res["is_glitch_candidate"] is True
    assert "GLITCH CONFIRMADO" in eval_res["verdict_summary"]


def test_accessory_iphone_case_discarded(filter_engine):
    """Verifica que una funda para iPhone sea clasificada como accesorio y descartada por precio normal."""
    title = "Funda Silicona Magnética MagSafe compatible con iPhone 15 Pro Max"
    eval_res = filter_engine.evaluate_glitch_candidate(
        title=title,
        current_price=9990.0,
        normal_price=19990.0,
    )
    sem = eval_res["semantic_result"]
    assert isinstance(sem, SemanticClassificationResult)
    assert sem.is_accessory is True
    assert sem.is_main_device is False
    assert sem.detected_item_type == "funda_carcasa"
    assert eval_res["is_glitch_candidate"] is False
    assert "ACCESORIO" in eval_res["verdict_summary"]
    assert "DESCARTADO" in eval_res["verdict_summary"]


def test_main_device_macbook_glitch(filter_engine):
    """Verifica que un MacBook Pro M3 sea clasificado como equipo principal y glitch confirmado."""
    title = "MacBook Pro 14 M3 512GB"
    eval_res = filter_engine.evaluate_glitch_candidate(
        title=title,
        current_price=249990.0,
        normal_price=1899990.0,
    )
    sem = eval_res["semantic_result"]
    assert isinstance(sem, SemanticClassificationResult)
    assert sem.is_main_device is True
    assert sem.is_accessory is False
    assert sem.detected_item_type == "notebook_laptop"
    assert eval_res["is_glitch_candidate"] is True
    assert "GLITCH CONFIRMADO" in eval_res["verdict_summary"]


def test_accessory_macbook_case_discarded(filter_engine):
    """Verifica que una carcasa para MacBook sea clasificada como accesorio y descartada."""
    title = "Carcasa Rigida Transparente para MacBook Pro 14 M3"
    eval_res = filter_engine.evaluate_glitch_candidate(
        title=title,
        current_price=14990.0,
        normal_price=29990.0,
    )
    sem = eval_res["semantic_result"]
    assert isinstance(sem, SemanticClassificationResult)
    assert sem.is_accessory is True
    assert sem.is_main_device is False
    assert sem.detected_item_type == "funda_carcasa"
    assert eval_res["is_glitch_candidate"] is False
    assert "DESCARTADO" in eval_res["verdict_summary"]


def test_screen_protector_glass(filter_engine):
    """Verifica detección de láminas y micas protectoras."""
    res = filter_engine.classify("Lámina Vidrio Templado 9D Para Samsung Galaxy S24 Ultra")
    assert res.is_accessory is True
    assert res.is_main_device is False
    assert res.detected_item_type == "lamina_mica_protector"


def test_cable_fast_charging(filter_engine):
    """Verifica detección de cables y adaptadores."""
    res = filter_engine.classify("Cable USB-C Carga Rápida 100W Para MacBook Pro")
    assert res.is_accessory is True
    assert res.is_main_device is False
    assert res.detected_item_type == "cable_adaptador"


def test_console_wall_mount(filter_engine):
    """Verifica detección de soportes de pared."""
    res = filter_engine.classify("Soporte de Pared Metálico Para PlayStation 5 PS5")
    assert res.is_accessory is True
    assert res.is_main_device is False
    assert res.detected_item_type == "soporte_stand"


def test_smartwatch_strap(filter_engine):
    """Verifica detección de correas de smartwatch."""
    res = filter_engine.classify("Correa Deportiva de Silicona compatible con Apple Watch Ultra")
    assert res.is_accessory is True
    assert res.is_main_device is False
    assert res.detected_item_type == "correa_pulsera"


def test_custom_llm_callable_integration():
    """Verifica inyección de función LLM simulada."""
    def mock_llm(title, category):
        return {
            "is_accessory": True,
            "is_main_device": False,
            "detected_item_type": "funda_carcasa",
            "explanation": "Clasificado por LLM mockeado.",
            "confidence": 0.99,
        }

    filter_llm = SemanticGlitchFilter(use_llm=True, custom_llm_callable=mock_llm)
    res = filter_llm.classify("Cualquier Producto Raro")
    assert res.is_accessory is True
    assert res.explanation == "Clasificado por LLM mockeado."
