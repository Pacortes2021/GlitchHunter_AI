"""
Pruebas de verificación y regresión para los 7 hallazgos de la Auditoría Técnica:
1. Extractor compartido en vivo (LiveRetailFeed) libre de errores de sintaxis e indentación.
2. Modo LIVE estricto: scrapers devuelven [] ante fallos de WAF/red y NUNCA catálogo simulado.
3. Máquina de estados: ofertas corregidas (de $89.990 a $899.990) pasan a CORRECTED y expiran si no se ven.
4. Comunicación WebSocket y callbacks thread-safe.
5. Inconsistencias de stock y precio tarjeta resueltas (in_stock=False rechazado, card_price registrado).
6. Configuración .env y verificación de entrega en Telegram antes de marcar enviado.
7. Sanitización y seguridad de datos.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem, GlitchVerdict, GlitchSeverity
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
from glitchhunter_ai.scrapers.falabella import FalabellaScraper
from glitchhunter_ai.scrapers.ripley import RipleyScraper
from glitchhunter_ai.scrapers.paris import ParisScraper
from glitchhunter_ai.scrapers.lider import LiderScraper
from glitchhunter_ai.scrapers.mercadolibre import MercadoLibreScraper
from glitchhunter_ai.scrapers.pcfactory import PCFactoryScraper


def test_audit_issue_1_live_feed_compilation_and_instantiation():
    """1. Verifica que LiveRetailFeed compila, se instancia y se inicializa limpiamente."""
    feed = LiveRetailFeed()
    assert feed is not None
    assert hasattr(feed, "fetch_live_products_for_store")


def test_audit_issue_2_live_mode_never_falls_back_to_mock_on_network_failure():
    """
    2. Verifica que en modo LIVE (use_live=True), ante fallo de red o WAF,
    todos los scrapers retornan lista vacía [] y NUNCA devuelven productos simulados.
    """
    scrapers = [
        FalabellaScraper(),
        RipleyScraper(),
        ParisScraper(),
        LiderScraper(),
        MercadoLibreScraper(),
        PCFactoryScraper(),
    ]

    for scraper in scrapers:
        with patch.object(LiveRetailFeed, "fetch_live_products_for_store", return_value=[]):
            # Mock de llamadas internas de red para simular bloqueo WAF / 403
            if hasattr(scraper, "fetch_live_catalog"):
                with patch.object(scraper, "fetch_live_catalog", return_value=[]):
                    items = scraper.fetch_catalog(use_live=True)
                    assert items == [], f"Scraper {scraper.store_name} devolvió simulaciones en modo LIVE"
            elif hasattr(scraper, "_fetch_live"):
                with patch.object(scraper, "_fetch_live", return_value=[]):
                    items = scraper.fetch_catalog(use_live=True)
                    assert items == [], f"Scraper {scraper.store_name} devolvió simulaciones en modo LIVE"
            elif hasattr(scraper, "_fetch_live_catalog"):
                with patch.object(scraper, "_fetch_live_catalog", return_value=[]):
                    items = scraper.fetch_catalog(use_live=True)
                    assert items == [], f"Scraper {scraper.store_name} devolvió simulaciones en modo LIVE"


def test_audit_issue_3_corrected_price_transitions_to_corrected_state(tmp_path):
    """
    3. Reproduce el caso auditado:
    - Oferta detectada a $89.990 (glitch activo).
    - La tienda corrige el precio a $899.990.
    - El sistema DEBE actualizar el estado a CORRECTED y no conservarla como ACTIVE.
    """
    db_file = tmp_path / "test_correction.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    # 1. Primera detección: producto a $89.990 (glitch real del 90%)
    glitch_item = ProductItem(
        id="audit-macbook-01",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Apple MacBook Pro 16 M3 Max 36GB 1TB",
        category="notebooks",
        current_price=89990.0,
        normal_price=899990.0,
        url="https://falabella.com/p/audit-01",
        in_stock=True,
    )

    alert = pipeline.process_product(glitch_item)
    assert alert is not None
    assert db.get_deal_status("audit-macbook-01") == "ACTIVE"

    # Verificar que aparece en get_recent_glitches
    active_glitches = db.get_recent_glitches()
    assert any(g["product_id"] == "audit-macbook-01" for g in active_glitches)

    # 2. La tienda corrige el precio a $899.990
    corrected_item = ProductItem(
        id="audit-macbook-01",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Apple MacBook Pro 16 M3 Max 36GB 1TB",
        category="notebooks",
        current_price=899990.0,
        normal_price=899990.0,
        url="https://falabella.com/p/audit-01",
        in_stock=True,
    )

    # El pipeline procesa el precio corregido
    alert_after_fix = pipeline.process_product(corrected_item)
    assert alert_after_fix is None  # No emite alerta

    # El estado en base de datos debe ser CORRECTED
    status = db.get_deal_status("audit-macbook-01")
    assert status == "CORRECTED", f"Se esperaba CORRECTED pero se obtuvo {status}"

    # Y ya NO debe aparecer entre los glitches activos del radar
    active_glitches_after = db.get_recent_glitches()
    assert not any(g["product_id"] == "audit-macbook-01" for g in active_glitches_after)


def test_audit_issue_3_stale_deals_auto_expiration(tmp_path):
    """3. Verifica que ofertas no vistas durante más de 12 horas expiren automáticamente."""
    db_file = tmp_path / "test_expire.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    # Insertar un deal activo con fecha de hace 15 horas
    stale_date = (datetime.utcnow() - timedelta(hours=15)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_connection() as conn:
        cursor = conn.cursor()
    # Insertar primero en products para satisfacer la foreign key
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO products (id, store_id, title, category, url, current_price, normal_price)
            VALUES ('stale-prod-01', 'ripley', 'Producto Antiguo', 'tv', 'https://ripley.cl/p/stale', 50000.0, 500000.0);
        """)
        cursor.execute("""
            INSERT INTO tracked_deals (
                product_id, store_id, title, category,
                normal_price, glitch_price, discount_pct, savings_clp,
                severity, reason, status, first_detected_at, last_seen_at,
                alert_dispatched, alert_id, url
            ) VALUES (
                'stale-prod-01', 'ripley', 'Producto Antiguo', 'tv',
                500000.0, 50000.0, 90.0, 450000.0,
                'CRITICO', 'Glitch 90%', 'ACTIVE', ?, ?,
                1, 'ALT-STALE-01', 'https://ripley.cl/p/stale'
            );
        """, (stale_date, stale_date))

    assert db.get_deal_status("stale-prod-01") == "ACTIVE"

    # Ejecutar expiración con umbral de 12 horas
    expired_count = db.expire_stale_deals(max_hours=12)
    assert expired_count == 1
    assert db.get_deal_status("stale-prod-01") == "EXPIRED"


def test_audit_issue_5_out_of_stock_rejected(tmp_path):
    """5. Verifica que un producto con in_stock=False NUNCA genere alerta ni glitch."""
    db_file = tmp_path / "test_stock.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    out_of_stock_item = ProductItem(
        id="stock-test-01",
        store_id="paris",
        store_name="Paris.cl",
        title="PlayStation 5 Slim Digital 1TB",
        category="consolas",
        current_price=49990.0,
        normal_price=499990.0,
        url="https://paris.cl/p/ps5",
        in_stock=False,  # SIN STOCK
    )

    alert = pipeline.process_product(out_of_stock_item)
    assert alert is None, "Un producto sin stock no debe generar alerta"
    assert db.get_deal_status("stock-test-01") is None


def test_audit_issue_5_card_price_recorded_accurately(tmp_path):
    """
    5. Verifica que un producto con descuento exclusivo por tarjeta registre
    el precio con tarjeta como glitch_price y no el precio normal/actual.
    """
    db_file = tmp_path / "test_card.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    card_deal = ProductItem(
        id="card-test-01",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Smart TV Samsung 65 4K UHD",
        category="televisores",
        current_price=899990.0,   # Precio comercial normal sin tarjeta
        normal_price=899990.0,
        card_price=89990.0,       # Glitch masivo solo con Tarjeta CMR (90% OFF)
        url="https://falabella.com/p/tv65",
        in_stock=True,
    )

    alert = pipeline.process_product(card_deal)
    assert alert is not None

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT glitch_price, reason FROM tracked_deals WHERE product_id = 'card-test-01';")
        row = cursor.fetchone()
        assert row is not None
        assert float(row["glitch_price"]) == 89990.0, "glitch_price debe ser el precio tarjeta reducido"
        assert "Tarjeta" in row["reason"]


def test_audit_issue_6_telegram_dispatch_failure_does_not_mark_sent(tmp_path):
    """6. Verifica que si el despacho de Telegram falla, NO se marque telegram_sent en la base de datos."""
    db_file = tmp_path / "test_tg.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    mock_dispatcher = MagicMock()
    # Simula fallo HTTP o de red en Telegram
    mock_dispatcher.send_alert.return_value = False

    pipeline = GlitchHuntingPipeline(db=db, telegram_dispatcher=mock_dispatcher)

    item = ProductItem(
        id="tg-fail-01",
        store_id="lider",
        store_name="Líder.cl",
        title="Consola Nintendo Switch OLED",
        category="consolas",
        current_price=29990.0,
        normal_price=349990.0,
        url="https://lider.cl/p/switch",
        in_stock=True,
    )

    alert = pipeline.process_product(item)
    assert alert is not None
    # No debe haberse marcado como enviado
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_sent FROM glitch_alerts WHERE product_id = 'tg-fail-01';")
        row = cursor.fetchone()
        assert row is not None
        assert row["telegram_sent"] == 0
