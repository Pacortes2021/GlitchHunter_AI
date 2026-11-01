"""
Test de desduplicación estricta de GlitchHunter AI.
Verifica que un mismo producto no genere alertas repetidas ni sature la base de datos
ni las notificaciones si se detecta de nuevo con el mismo precio.
"""

import pytest
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline


def test_duplicate_alert_suppression():
    db = PriceDatabase(":memory:")
    pipeline = GlitchHuntingPipeline(db=db)

    # 0. Historial base previo del producto a precio normal
    normal_item = ProductItem(
        id="test-dedup-001",
        store_id="falabella",
        store_name="Falabella",
        title="Apple iPhone 15 Pro Max 256GB Titanio",
        category="smartphones",
        current_price=899990.0,
        normal_price=899990.0,
        url="https://www.falabella.com/producto/123",
        in_stock=True
    )
    db.upsert_product(normal_item)
    db.upsert_product(normal_item)

    # 1. Primera detección del glitch ($89.990): debe emitir alerta
    product_glitch = ProductItem(
        id="test-dedup-001",
        store_id="falabella",
        store_name="Falabella",
        title="Apple iPhone 15 Pro Max 256GB Titanio",
        category="smartphones",
        current_price=89990.0,
        normal_price=899990.0,
        url="https://www.falabella.com/producto/123",
        in_stock=True
    )

    alert1 = pipeline.process_product(product_glitch)
    assert alert1 is not None
    assert alert1.product.id == "test-dedup-001"

    # Verificar que quedó 1 alerta en base de datos
    glitches = db.get_recent_glitches()
    assert len(glitches) == 1
    assert glitches[0]["product_id"] == "test-dedup-001"

    # 2. Segunda detección (siguiente ciclo del escáner a los 60s con el mismo precio de $89.990):
    # Debe ser detectado como duplicado y retornar None (sin re-alertar ni duplicar en DB)
    alert2 = pipeline.process_product(product_glitch)
    assert alert2 is None

    # Verificar que sigue habiendo exactamente 1 alerta en DB
    glitches_after = db.get_recent_glitches()
    assert len(glitches_after) == 1

    # 3. Verificación directa de is_duplicate_alert
    assert db.is_duplicate_alert("test-dedup-001", 89990.0) is True
    # Si el precio baja significativamente más (ej: a $59.990):
    assert db.is_duplicate_alert("test-dedup-001", 59990.0) is False


def test_continuous_reviewer_state_machine():
    """
    Prueba el ciclo de vida continuo de excelencia:
    - 1er ciclo: detecta y registra como nuevo (is_new=True).
    - 2do ciclo: reconoce que ya está activo y lo mantiene sin emitir alerta (is_new=False).
    - 3er ciclo: corrección por parte de la tienda (status='CORRECTED', is_new=False).
    """
    db = PriceDatabase(":memory:")
    pipeline = GlitchHuntingPipeline(db=db)

    item = ProductItem(
        id="test-state-001",
        store_id="ripley",
        store_name="Ripley",
        title="Consola PlayStation 5 Slim 1TB",
        category="consolas",
        current_price=49990.0,
        normal_price=499990.0,
        url="https://simple.ripley.cl/ps5",
        in_stock=True
    )

    # 1. Descubrimiento inicial
    alert = pipeline.process_product(item)
    assert alert is not None
    assert len(db.get_recent_glitches()) == 1

    # 2. Re-lectura del catálogo en el siguiente ciclo (mismo precio)
    alert_repeat = pipeline.process_product(item)
    assert alert_repeat is None
    # Permanece en el catálogo activo sin duplicados
    assert len(db.get_recent_glitches()) == 1

    # 3. La tienda corrige el precio de vuelta a normal
    item_corrected = ProductItem(
        id="test-state-001",
        store_id="ripley",
        store_name="Ripley",
        title="Consola PlayStation 5 Slim 1TB",
        category="consolas",
        current_price=499990.0,
        normal_price=499990.0,
        url="https://simple.ripley.cl/ps5",
        in_stock=True
    )
    alert_corrected = pipeline.process_product(item_corrected)
    assert alert_corrected is None
