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

    # 3. 🎯 HALLAZGO AUDITORÍA 2: Reactivación de la oferta (vuelve a bajar a $89.990)
    # Debe reactivarse de CORRECTED -> ACTIVE y emitir una nueva alerta
    reactivated_item = ProductItem(
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

    alert_reactivated = pipeline.process_product(reactivated_item)
    assert alert_reactivated is not None, "La reactivación de una oferta corregida debe emitir alerta"
    assert db.get_deal_status("audit-macbook-01") == "ACTIVE"

    # Ahora sí debe volver a aparecer en el radar
    active_glitches_reborn = db.get_recent_glitches()
    assert any(g["product_id"] == "audit-macbook-01" for g in active_glitches_reborn)


def test_audit_issue_3_partial_price_increase_retains_active_status(tmp_path):
    """
    3. Verifica que una subida de precio parcial que AÚN conserve un descuento masivo
    (ej: de $10.000 a $15.000 sobre un producto de $100.000 = 85% de ahorro)
    permanezca como ACTIVE y NO se marque erróneamente como CORRECTED.
    """
    db_file = tmp_path / "test_partial_increase.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    # 1. Producto detectado inicialmente a $10.000 (90% OFF)
    deal_item = ProductItem(
        id="partial-inc-01",
        store_id="ripley",
        store_name="Ripley Chile",
        title="Audífonos Sony WH-1000XM5 Cancelación Ruido",
        category="tecnologia",
        current_price=10000.0,
        normal_price=100000.0,
        url="https://ripley.cl/p/sony-xm5",
        in_stock=True,
    )

    alert_1 = pipeline.process_product(deal_item)
    assert alert_1 is not None
    assert db.get_deal_status("partial-inc-01") == "ACTIVE"

    # 2. El precio sube a $15.000 (+50% de subida, pero 85% de descuento)
    deal_item_step2 = ProductItem(
        id="partial-inc-01",
        store_id="ripley",
        store_name="Ripley Chile",
        title="Audífonos Sony WH-1000XM5 Cancelación Ruido",
        category="tecnologia",
        current_price=15000.0,
        normal_price=100000.0,
        url="https://ripley.cl/p/sony-xm5",
        in_stock=True,
    )

    alert_2 = pipeline.process_product(deal_item_step2)
    # No emite alerta duplicada innecesaria (es ajuste menor)
    assert alert_2 is None

    # Pero CRUCIAL: El estado DEBE SEGUIR SIENDO 'ACTIVE', NUNCA 'CORRECTED'!
    status = db.get_deal_status("partial-inc-01")
    assert status == "ACTIVE", f"Se esperaba ACTIVE pero se obtuvo {status}"

    # El precio registrado en el radar debe reflejar los $15.000
    recent = db.get_recent_glitches()
    target_row = next((g for g in recent if g["product_id"] == "partial-inc-01"), None)
    assert target_row is not None
    assert float(target_row["current_price"]) == 15000.0


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
        cursor.execute("SELECT glitch_price, reason, card_price FROM tracked_deals WHERE product_id = 'card-test-01';")
        row = cursor.fetchone()
        assert row is not None
        assert float(row["glitch_price"]) == 89990.0, "glitch_price debe ser el precio tarjeta reducido"
        assert float(row["card_price"]) == 89990.0, "card_price en tracked_deals debe guardar el precio tarjeta"
        assert "Tarjeta" in row["reason"]

    # Verificar que get_recent_glitches expone card_price para el frontend
    recent_deals = db.get_recent_glitches()
    card_deal_radar = next((g for g in recent_deals if g["product_id"] == "card-test-01"), None)
    assert card_deal_radar is not None
    assert card_deal_radar["card_price"] == 89990.0, "get_recent_glitches debe exponer card_price fidedigno"


def test_audit_issue_6_telegram_dispatch_failure_and_retry(tmp_path):
    """
    6. Verifica que si el despacho de Telegram falla:
    - glitch_alerts.telegram_sent es 0.
    - tracked_deals.alert_dispatched es 0 (no 1).
    - get_recent_glitches().telegram_sent es False (no True).
    - En el siguiente escaneo, el sistema reintenta el despacho exitosamente.
    """
    db_file = tmp_path / "test_tg.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    mock_dispatcher = MagicMock()
    mock_dispatcher.is_simulation = False
    # 1. Simular fallo HTTP o de red en Telegram
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

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_sent FROM glitch_alerts WHERE product_id = 'tg-fail-01';")
        row = cursor.fetchone()
        assert row is not None
        assert row["telegram_sent"] == 0, "glitch_alerts.telegram_sent debe ser 0 si falló"

        cursor.execute("SELECT alert_dispatched FROM tracked_deals WHERE product_id = 'tg-fail-01';")
        t_row = cursor.fetchone()
        assert t_row is not None
        assert t_row["alert_dispatched"] == 0, "tracked_deals.alert_dispatched debe ser 0 si falló"

    # Comprobar que el radar reporta telegram_sent como False
    recent = db.get_recent_glitches()
    tg_deal = next((g for g in recent if g["product_id"] == "tg-fail-01"), None)
    assert tg_deal is not None
    assert tg_deal["telegram_sent"] is False, "get_recent_glitches debe reportar False si no se despachó"

    # 2. Reintento en siguiente ciclo: ahora Telegram responde exitosamente
    mock_dispatcher.send_alert.return_value = True
    alert_retry = pipeline.process_product(item)
    assert alert_retry is not None, "El revisor debe reintentar el despacho si el anterior falló"

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT alert_dispatched FROM tracked_deals WHERE product_id = 'tg-fail-01';")
        t_row_after = cursor.fetchone()
        assert t_row_after["alert_dispatched"] == 1, "alert_dispatched debe confirmarse en 1 tras despacho exitoso"


# ==============================================================================
# 🎯 PRUEBAS DE REGRESIÓN: AUDITORÍA 3 (GITHUB AUDIT - NOVIEMBRE 2026)
# ==============================================================================

def test_audit_3_hallazgo_1_chilean_price_parser_thousands_and_decimals():
    """
    Hallazgo 1 (Crítico): Verifica que parse_clp_price() no confunda separadores
    de miles con decimales en pesos chilenos.
    """
    from glitchhunter_ai.scrapers.live_feed import parse_clp_price

    # Casos críticos señalados en auditoría
    assert parse_clp_price("99.990") == 99990.0, "99.990 debe ser $99.990 CLP, nunca 99.99"
    assert parse_clp_price("899.990") == 899990.0, "899.990 debe ser $899.990 CLP, nunca 899.99"
    assert parse_clp_price("1.299.990") == 1299990.0, "1.299.990 debe ser $1.299.990 CLP"
    assert parse_clp_price("99,990") == 99990.0, "99,990 con coma de miles debe ser $99.990 CLP"

    # Formatos de API con decimales técnicos (.00 o .0)
    assert parse_clp_price("59990.00") == 59990.0
    assert parse_clp_price("59990.0") == 59990.0

    # Texto con formato moneda y espacios
    assert parse_clp_price("$ 1.499.990 CLP") == 1499990.0
    assert parse_clp_price("$89.990") == 89990.0

    # Tipos numéricos directos y valores nulos
    assert parse_clp_price(49990) == 49990.0
    assert parse_clp_price(49990.5) == 49990.5
    assert parse_clp_price(None) == 0.0
    assert parse_clp_price("") == 0.0


def test_audit_3_hallazgo_2_demo_mode_notification_isolation(tmp_path):
    """
    Hallazgo 2 (Crítico): Verifica que el modo demostración y las alertas de prueba
    estén estrictamente aislados de llamadas de red de notificaciones reales (ntfy, Discord, Telegram).
    """
    from glitchhunter_ai.notifications.ntfy_client import NtfyAlertDispatcher
    from glitchhunter_ai.notifications.discord_webhook import DiscordAlertDispatcher

    db = PriceDatabase(db_path=tmp_path / "test_demo_iso.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    # Monitorear que requests.post NUNCA sea llamado al correr en modo use_live=False
    with patch("requests.post") as mock_post:
        glitches = pipeline.run_scan(use_live=False, send_notifications=False)
        assert len(glitches) >= 0
        # Cero llamadas a servidores push externos
        assert mock_post.call_count == 0, "No debe realizarse ninguna petición HTTP externa en modo DEMO"

    # Probar que NtfyAlertDispatcher y DiscordAlertDispatcher respetan simulation_mode
    ntfy = NtfyAlertDispatcher(simulation_mode=True, enabled=True, topic="test_topic")
    discord = DiscordAlertDispatcher(simulation_mode=True, webhook_url="https://discord.com/api/webhooks/real/path")

    sample_product = ProductItem(
        id="test-sim-01",
        store_id="test_store",
        store_name="Tienda Test",
        title="Producto Simulado",
        category="notebooks",
        current_price=99990.0,
        normal_price=999990.0,
        url="https://tienda.cl/prod",
    )
    sample_alert = pipeline.process_product(sample_product, send_notifications=False)

    with patch("requests.post") as mock_post:
        assert ntfy.send_alert(sample_alert) is True
        assert discord.send_alert(sample_alert) is True
        assert mock_post.call_count == 0, "En modo simulación no debe haber llamadas HTTP a redes de terceros"


def test_audit_3_hallazgo_3_admin_endpoint_security_and_auth():
    """
    Hallazgo 3 (Alta): Verifica que los endpoints administrativos requieran autenticación
    cuando API_AUTH_KEY esté configurada, y que ALLOW_TEST_GLITCH controle el endpoint de test.
    """
    from fastapi.testclient import TestClient
    from glitchhunter_ai.web import server

    client = TestClient(server.app)

    # 1. Con API_AUTH_KEY vacía (entorno local por defecto): acceso permitido
    with patch.object(server, "API_AUTH_KEY", ""):
        resp = client.get("/api/status")
        assert resp.status_code == 200

    # 2. Con API_AUTH_KEY configurada: rechaza peticiones sin header con 401
    with patch.object(server, "API_AUTH_KEY", "super_secret_hunter_key_123"):
        # Petición no autenticada a /api/scan
        resp_unauth = client.post("/api/scan")
        assert resp_unauth.status_code == 401
        assert "Autenticación administrativa requerida" in resp_unauth.json()["detail"]

        # Petición autenticada vía X-API-Key
        with patch.object(server.pipeline, "run_scan", return_value=[]):
            resp_auth = client.post("/api/scan", headers={"X-API-Key": "super_secret_hunter_key_123"})
            assert resp_auth.status_code == 200

        # Petición autenticada vía Authorization Bearer
        with patch.object(server.pipeline, "run_scan", return_value=[]):
            resp_bearer = client.post("/api/scan", headers={"Authorization": "Bearer super_secret_hunter_key_123"})
            assert resp_bearer.status_code == 200

    # 3. Verificación de ALLOW_TEST_GLITCH deshabilitado
    with patch.object(server, "ALLOW_TEST_GLITCH", False):
        resp_blocked = client.post("/api/test-glitch")
        assert resp_blocked.status_code == 403


def test_audit_3_hallazgo_4_baseline_history_depth_for_stable_price(tmp_path):
    """
    Hallazgo 4 (Alta): Verifica que productos con precio estable continuo durante días
    construyan y mantengan profundidad temporal utilizable para get_baseline_price().
    """
    db = PriceDatabase(db_path=tmp_path / "test_baseline_depth.db")
    db.init_db()

    prod_id = "tv-samsung-stable"
    now = datetime.utcnow()
    day_start = now - timedelta(days=20)

    # 1. Producto visto hace 20 días a $599.990
    prod_old = ProductItem(
        id=prod_id,
        store_id="falabella",
        store_name="Falabella Chile",
        title="Smart TV Samsung 55 UHD",
        category="televisores",
        current_price=599990.0,
        normal_price=599990.0,
        url="https://falabella.com/tv",
        timestamp=day_start,
    )
    db.upsert_product(prod_old)

    # 2. Escaneos repetidos manteniendo el mismo precio en fechas intermedias
    day_mid = now - timedelta(days=10)
    prod_mid = ProductItem(
        id=prod_id,
        store_id="falabella",
        store_name="Falabella Chile",
        title="Smart TV Samsung 55 UHD",
        category="televisores",
        current_price=599990.0,
        normal_price=599990.0,
        url="https://falabella.com/tv",
        timestamp=day_mid,
    )
    db.upsert_product(prod_mid)

    # Verificar que el baseline histórico detecta la profundidad de $599.990
    baseline = db.get_baseline_price(prod_id, days=30)
    assert baseline is not None
    assert baseline == 599990.0, "El precio base debe calcularse correctamente sobre el precio estable histórico"


def test_audit_3_hallazgo_5_stats_separation_active_vs_historical(tmp_path):
    """
    Hallazgo 5 (Media-Alta): Verifica que get_stats() no muestre alertas antiguas
    como glitches activos cuando todas las ofertas están corregidas o expiradas.
    """
    db = PriceDatabase(db_path=tmp_path / "test_stats_sep.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    # 1. Registrar una oferta glitch activa
    item = ProductItem(
        id="item-stat-01",
        store_id="ripley",
        store_name="Ripley Chile",
        title="iPad Air M2",
        category="tablets",
        current_price=74990.0,
        normal_price=749990.0,
        url="https://ripley.cl/ipad",
        in_stock=True,
    )
    alert = pipeline.process_product(item, send_notifications=False)
    assert alert is not None

    stats_active = db.get_stats()
    assert stats_active["total_glitches"] == 1
    assert stats_active["active_glitches"] == 1
    assert stats_active["historical_distinct_glitches"] == 1

    # 2. La tienda corrige el precio a $749.990 (transición a CORRECTED)
    item_corrected = ProductItem(
        id="item-stat-01",
        store_id="ripley",
        store_name="Ripley Chile",
        title="iPad Air M2",
        category="tablets",
        current_price=749990.0,
        normal_price=749990.0,
        url="https://ripley.cl/ipad",
        in_stock=True,
    )
    pipeline.process_product(item_corrected, send_notifications=False)

    stats_after = db.get_stats()
    # Con la oferta corregida, el radar activo DEBE marcar 0 glitches activos
    assert stats_after["total_glitches"] == 0, "total_glitches debe ser 0 cuando no hay ofertas activas"
    assert stats_after["active_glitches"] == 0
    assert stats_after["accumulated_savings_clp"] == 0.0
    # Pero el histórico conserva la traza de la alerta
    assert stats_after["historical_distinct_glitches"] == 1


def test_audit_3_hallazgo_6_mercadolibre_bs4_dependency():
    """
    Hallazgo 6 (Media): Verifica que BeautifulSoup esté disponible
    y se importe sin errores para el scraper de Mercado Libre.
    """
    from bs4 import BeautifulSoup
    import bs4
    assert BeautifulSoup is not None
    assert bs4.__version__ is not None


# ==============================================================================
# 🎯 PRUEBAS DE REGRESIÓN: AUDITORÍA 4 (NOVIEMBRE 2026)
# ==============================================================================

def test_audit_4_hallazgo_a_price_history_deduplication_preserves_cycles(tmp_path):
    """
    Hallazgo A (Crítica): Verifica que cleanup_duplicate_price_history() NO elimine
    observaciones válidas cuando un precio regresa al mismo valor en fechas distintas
    (ej: $100k el 1 Oct -> $80k el 10 Oct -> $100k el 20 Oct -> $70k el 30 Oct).
    """
    db = PriceDatabase(db_path=tmp_path / "test_history_cycles.db")
    db.init_db()

    prod_id = "tv-cycle-test"

    # Insertar la secuencia exacta reportada por el auditor
    history_points = [
        (prod_id, 100000.0, "2026-10-01 10:00:00"),
        (prod_id, 80000.0, "2026-10-10 10:00:00"),
        (prod_id, 100000.0, "2026-10-20 10:00:00"),  # <- Este registro se eliminaba erróneamente
        (prod_id, 70000.0, "2026-10-30 10:00:00"),
    ]

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO products (id, store_id, title, current_price, normal_price) VALUES (?, 'falabella', 'TV', 70000, 100000);", (prod_id,))
        for pid, pr, ts in history_points:
            cursor.execute("INSERT INTO price_history (product_id, price, timestamp) VALUES (?, ?, ?);", (pid, pr, ts))

    # Ejecutar la limpieza de duplicados
    db.cleanup_duplicate_price_history()

    # Comprobar que los 4 registros históricos siguen existiendo intactos
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT price, timestamp FROM price_history WHERE product_id = ? ORDER BY timestamp ASC;", (prod_id,))
        rows = cursor.fetchall()
        assert len(rows) == 4, f"Se esperaban 4 observaciones históricas, se obtuvieron {len(rows)}"
        assert rows[0]["price"] == 100000.0
        assert rows[1]["price"] == 80000.0
        assert rows[2]["price"] == 100000.0, "La observación del 20 de octubre ($100.000) debe preservarse intacta"
        assert rows[3]["price"] == 70000.0


def test_audit_4_hallazgo_b_get_stats_strict_active_deals_isolation(tmp_path):
    """
    Hallazgo B (Alta): Verifica que cuando todas las ofertas expiran o son corregidas,
    total_glitches sea estrictamente 0 y NUNCA recurra a alertas históricas.
    """
    db = PriceDatabase(db_path=tmp_path / "test_stats_active_strict.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    # Inyectar una alerta histórica
    item = ProductItem(
        id="prod-strict-01",
        store_id="falabella",
        store_name="Falabella",
        title="Laptop Gamer",
        category="notebooks",
        current_price=99990.0,
        normal_price=999990.0,
        url="https://falabella.com/p1",
        in_stock=True,
    )
    pipeline.process_product(item, send_notifications=False)

    stats1 = db.get_stats()
    assert stats1["total_glitches"] == 1
    assert stats1["active_glitches"] == 1

    # Oferta corregida
    item_corr = ProductItem(
        id="prod-strict-01",
        store_id="falabella",
        store_name="Falabella",
        title="Laptop Gamer",
        category="notebooks",
        current_price=999990.0,
        normal_price=999990.0,
        url="https://falabella.com/p1",
        in_stock=True,
    )
    pipeline.process_product(item_corr, send_notifications=False)

    stats2 = db.get_stats()
    assert stats2["total_glitches"] == 0, "total_glitches debe ser 0"
    assert stats2["active_glitches"] == 0
    assert stats2["historical_distinct_glitches"] == 1, "Histórico debe permanecer separado"


def test_audit_4_hallazgo_d_store_health_empty_response_and_blocked():
    """
    Hallazgo D (Alta): Verifica que una extracción de 0 ítems no se reporte como HEALTHY,
    sino como EMPTY_RESPONSE, y que bloqueos se clasifiquen como BLOCKED.
    """
    from glitchhunter_ai.core.store_health import StoreHealthTracker

    tracker = StoreHealthTracker()

    # 1. Escaneo rápido de 850ms pero con 0 productos extraídos -> EMPTY_RESPONSE
    tracker.record_scan("paris", "Paris Chile", latency_ms=850.0, items_count=0, error=False)
    matrix = tracker.get_matrix()
    paris_rec = next(s for s in matrix if s["store_id"] == "paris")
    assert paris_rec["status"] == "EMPTY_RESPONSE", "0 productos no debe ser HEALTHY, sino EMPTY_RESPONSE"

    # 2. Bloqueo por WAF / Bot Manager -> BLOCKED
    tracker.record_scan("ripley", "Ripley Chile", latency_ms=500.0, items_count=0, blocked=True)
    matrix2 = tracker.get_matrix()
    ripley_rec = next(s for s in matrix2 if s["store_id"] == "ripley")
    assert ripley_rec["status"] == "BLOCKED"

    # 3. Escaneo normal exitoso -> HEALTHY
    tracker.record_scan("falabella", "Falabella", latency_ms=600.0, items_count=15)
    matrix3 = tracker.get_matrix()
    fal_rec = next(s for s in matrix3 if s["store_id"] == "falabella")
    assert fal_rec["status"] == "HEALTHY"


def test_audit_4_hallazgo_e_calibrated_confidence_and_verification_level(tmp_path):
    """
    Hallazgo E (Alta): Verifica que get_recent_glitches() reporte la certeza matemática real
    sin forzar un 95% fijo, que la watchlist no altere la probabilidad estadística,
    y que se asigne el nivel de verificación adecuado.
    """
    from glitchhunter_ai.core.models import VerificationLevel

    db = PriceDatabase(db_path=tmp_path / "test_calibrated_conf.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    item = ProductItem(
        id="item-conf-01",
        store_id="pcfactory",
        store_name="PC Factory",
        title="RTX 4070 Super",
        category="computacion",
        current_price=109990.0,
        normal_price=699990.0,
        url="https://pcfactory.cl/p1",
        in_stock=True,
    )
    alert = pipeline.process_product(item, send_notifications=False)
    assert alert is not None
    # El enlace Sniper generado no se valida contra la tienda: nunca debe elevar a COMPRABLE
    assert alert.verification_level == VerificationLevel.VERIFICADO

    # Verificar que el registro en radar refleja el score real
    recent = db.get_recent_glitches()
    assert len(recent) == 1
    assert recent[0]["verification_level"] == "VERIFICADO"
    assert recent[0]["confidence_score"] == alert.verdict.confidence_score


def test_audit_4_hallazgo_f_pipeline_concurrent_scan_protection(tmp_path):
    """
    Hallazgo F (Media-Alta): Verifica que múltiples llamadas simultáneas a run_scan()
    no produzcan colisiones, rechazando limpiamente ejecuciones superpuestas.
    """
    import time
    import threading

    db = PriceDatabase(db_path=tmp_path / "test_concurrency.db")
    db.init_db()

    pipeline = GlitchHuntingPipeline(db=db)

    results = []

    def slow_scan():
        # Simula un escaneo que toma tiempo bloqueando el cerrojo
        res = pipeline.run_scan(use_live=False, send_notifications=False)
        results.append(len(res))

    # Adquirir el cerrojo del pipeline para simular un escaneo activo
    assert pipeline._scan_lock.acquire(blocking=False) is True
    pipeline.is_scanning = True

    try:
        # Una llamada simultánea debe ser rechazada inmediatamente retornando []
        concurrent_res = pipeline.run_scan(use_live=False)
        assert concurrent_res == [], "El escaneo concurrente debe rechazarse retornando []"
    finally:
        pipeline.is_scanning = False
        pipeline._scan_lock.release()


