"""
Tests exhaustivos para las nuevas funcionalidades avanzadas de GlitchHunter AI:
1. SoloTodo Meta-Scraper con concurrencia y latencia ultrabaja.
2. Evasión de bloqueos con huella TLS Chrome (curl_cffi / StealthHTTPClient).
3. Modo Sniper (Add-to-Cart en 1 Clic) y Matriz de Salud de Retailers (Store Health).
4. Despachador de Discord Webhooks y Watchlist de palabras clave prioritarias.
"""

import time
import pytest
from datetime import datetime

from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    ProductItem,
    SemanticClassificationResult,
)
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.core.cart_sniper import generate_direct_add_to_cart_url
from glitchhunter_ai.core.store_health import StoreHealthTracker, global_store_health
from glitchhunter_ai.scrapers.solotodo import SoloTodoScraper
from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
from glitchhunter_ai.scrapers.stealth_client import (
    StealthHTTPClient,
    default_stealth_client,
    DEFAULT_CHROME_HEADERS,
)
from glitchhunter_ai.notifications.discord_webhook import DiscordAlertDispatcher


# =========================================================================
# 1. PRUEBAS DE SOLOTODO META-SCRAPER
# =========================================================================

def test_solotodo_metadata_and_simulation():
    """Valida los metadatos y catálogo simulado de SoloTodo."""
    scraper = SoloTodoScraper()
    assert scraper.store_id == "solotodo"
    assert "SoloTodo" in scraper.store_name

    catalog = scraper.fetch_catalog(use_live=False)
    assert len(catalog) >= 3
    for p in catalog:
        assert isinstance(p, ProductItem)
        assert p.store_id == "solotodo"
        assert p.current_price > 0
        assert p.normal_price >= p.current_price


def test_solotodo_latency_is_under_threshold():
    """Verifica que la latencia del escáner SoloTodo se mantenga por debajo del umbral de rendimiento."""
    scraper = SoloTodoScraper()
    t0 = time.perf_counter()
    catalog = scraper.fetch_catalog(use_live=False)
    elapsed = (time.perf_counter() - t0) * 1000.0

    assert elapsed < 1000.0, f"Latencia excesiva en SoloTodo: {elapsed:.1f}ms"
    assert scraper.last_latency_ms >= 0.0


# =========================================================================
# 2. PRUEBAS DE EVASIÓN DE BLOQUEOS (STEALTH HTTP CLIENT)
# =========================================================================

def test_stealth_client_headers_and_initialization():
    """Verifica la configuración de cabeceras Chrome 124 y sesión stealth."""
    client = StealthHTTPClient(impersonate="chrome124")
    assert client.impersonate == "chrome124"
    assert client.timeout > 0

    # Verificar que el singleton default está disponible
    assert default_stealth_client is not None
    assert hasattr(default_stealth_client, "get")
    assert hasattr(default_stealth_client, "post")


def test_stealth_client_chrome124_headers_integrity():
    """Verifica que las cabeceras emulen fehacientemente un navegador Chrome 124 en macOS."""
    headers = DEFAULT_CHROME_HEADERS
    assert "Chrome/124.0.0.0" in headers["User-Agent"]
    assert '"Chromium";v="124"' in headers["Sec-Ch-Ua"]
    assert '"Google Chrome";v="124"' in headers["Sec-Ch-Ua"]
    assert headers["Sec-Ch-Ua-Mobile"] == "?0"
    assert headers["Sec-Ch-Ua-Platform"] == '"macOS"'
    assert "gzip, deflate, br, zstd" in headers["Accept-Encoding"]
    assert "es-CL" in headers["Accept-Language"]


def test_stealth_client_curl_cffi_impersonation():
    """Verifica la compatibilidad con curl_cffi para suplantación JA3/JA4."""
    client = StealthHTTPClient(impersonate="chrome124")
    if client.use_curl_cffi:
        assert client._session is not None
        # Validar llamada mockeada asegurando que impersonate y cabeceras se transmiten
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client._session.get = MagicMock(return_value=mock_resp)

        resp = client.get("https://example.com/api/test", headers={"X-Custom": "123"})
        assert resp.status_code == 200
        client._session.get.assert_called_once()
        call_kwargs = client._session.get.call_args[1]
        assert call_kwargs["impersonate"] == "chrome124"
        assert call_kwargs["headers"]["X-Custom"] == "123"
        assert "Chrome/124.0.0.0" in call_kwargs["headers"]["User-Agent"]


def test_stealth_client_session_failure_fallback_to_direct_curl():
    """Verifica que si la sesión curl_cffi falla, reintenta directamente con curl_requests."""
    from unittest.mock import MagicMock, patch
    client = StealthHTTPClient(impersonate="chrome124")
    if client.use_curl_cffi:
        client._session.get = MagicMock(side_effect=Exception("Session connection pool broken"))
        mock_direct_resp = MagicMock()
        mock_direct_resp.status_code = 200

        with patch("glitchhunter_ai.scrapers.stealth_client.curl_requests.get", return_value=mock_direct_resp) as mock_get:
            resp = client.get("https://example.com/test-failover")
            assert resp.status_code == 200
            mock_get.assert_called_once()


def test_stealth_client_full_fallback_to_std_requests():
    """Verifica que si curl_cffi falla por completo, existe un fallback transparente a requests estándar."""
    from unittest.mock import MagicMock, patch
    client = StealthHTTPClient(impersonate="chrome124")
    client.use_curl_cffi = True

    client._session = MagicMock()
    client._session.get.side_effect = Exception("Curl CFFI crashed")
    
    mock_std_resp = MagicMock()
    mock_std_resp.status_code = 200

    with patch("glitchhunter_ai.scrapers.stealth_client.curl_requests.get", side_effect=Exception("Direct curl failed")):
        with patch("glitchhunter_ai.scrapers.stealth_client.std_requests.get", return_value=mock_std_resp) as mock_std_get:
            resp = client.get("https://example.com/ultimate-fallback")
            assert resp.status_code == 200
            mock_std_get.assert_called_once()


def test_stealth_client_fallback_mode_without_curl_cffi():
    """Verifica la degradación suave cuando curl_cffi no está instalado."""
    from unittest.mock import MagicMock, patch
    with patch("glitchhunter_ai.scrapers.stealth_client._CURL_CFFI_AVAILABLE", False):
        client = StealthHTTPClient(impersonate="chrome124")
        assert client.use_curl_cffi is False
        assert client._session is not None

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        client._session.get = MagicMock(return_value=mock_resp)

        resp = client.get("https://example.com/no-curl")
        assert resp.status_code == 200
        client._session.get.assert_called_once()


def test_stealth_client_integration_with_live_feed():
    """Verifica que LiveRetailFeed utiliza default_stealth_client."""
    feed = LiveRetailFeed()
    assert feed.session is default_stealth_client
    assert hasattr(feed.session, "get")


def test_stealth_client_integration_with_solotodo():
    """Verifica que SoloTodoScraper utiliza default_stealth_client en escaneos paralelos."""
    scraper = SoloTodoScraper()
    assert scraper.client is default_stealth_client
    assert hasattr(scraper.client, "get")


# =========================================================================
# 3. PRUEBAS DE MODO SNIPER (1-CLIC ADD-TO-CART) Y STORE HEALTH
# =========================================================================

def test_sniper_add_to_cart_url_generation():
    """Verifica la deducción y generación de links directos de compra para cada retail."""
    # 1. Falabella (URL directa y fallback a product_id)
    fala_url = "https://www.falabella.com/falabella-cl/product/16912389/Smart-TV-Samsung-OLED/16912389"
    cart_fala = generate_direct_add_to_cart_url("falabella", fala_url)
    assert "checkout?skuId=16912389" in cart_fala

    cart_fala_fallback = generate_direct_add_to_cart_url("falabella", "https://www.falabella.com/falabella-cl", product_id="16912389")
    assert "checkout?skuId=16912389" in cart_fala_fallback

    # 2. Ripley (VTEX)
    ripley_url = "https://simple.ripley.cl/lg-oled-65-2000390111p"
    cart_ripley = generate_direct_add_to_cart_url("ripley", ripley_url)
    assert "/checkout/cart/add?sku=2000390111" in cart_ripley

    # 3. Líder
    lider_url = "https://www.lider.cl/catalogo/product/sku/1029381/lg-smart-tv-55"
    cart_lider = generate_direct_add_to_cart_url("lider", lider_url)
    assert "/cart/add?sku=1029381" in cart_lider

    # 4. PC Factory
    pcf_url = "https://www.pcfactory.cl/producto/48912-nvidia-rtx4080super"
    cart_pcf = generate_direct_add_to_cart_url("pcfactory", pcf_url)
    assert "carro/agregar?id=48912" in cart_pcf

    # 5. Paris (Cencosud)
    paris_url = "https://www.paris.cl/smart-tv-lg-oled-evo-55-4k-uhd-oled55c3psa-883921999.html"
    cart_paris = generate_direct_add_to_cart_url("paris", paris_url)
    assert "/cart-add?pid=883921999&quantity=1" in cart_paris

    cart_paris_fallback = generate_direct_add_to_cart_url("paris", "https://www.paris.cl/landing", product_id="paris-883921999")
    assert "/cart-add?pid=883921999&quantity=1" in cart_paris_fallback

    # 6. Mercado Libre
    mlc_url = "https://articulo.mercadolibre.cl/MLC-987654321-smart-tv.html"
    cart_mlc = generate_direct_add_to_cart_url("mercadolibre", mlc_url)
    assert "comprar-ahora?itemId=MLC987654321" in cart_mlc

    cart_mlc_fallback = generate_direct_add_to_cart_url("mercadolibre", "https://articulo.mercadolibre.cl/search", product_id="MLC-987654321")
    assert "comprar-ahora?itemId=MLC987654321" in cart_mlc_fallback

    # 7. Tiendas Shopify (ej: SP Digital)
    sp_url = "https://www.spdigital.cl/products/tarjeta-nvidia-rtx-4090"
    cart_sp = generate_direct_add_to_cart_url("spdigital", sp_url)
    assert "/cart/add?url=" in cart_sp

    # 8. SoloTodo (debe mantener el link original de ficha para redirigir a tienda)
    solo_url = "https://www.solotodo.cl/products/209184-asus-rog-strix-scar-16"
    cart_solo = generate_direct_add_to_cart_url("solotodo", solo_url)
    assert cart_solo == solo_url

    # 9. Casos límite (None, URLs vacías o sin formato)
    assert generate_direct_add_to_cart_url("falabella", "") is None
    assert generate_direct_add_to_cart_url("desconocida", "https://tienda.cl/item") == "https://tienda.cl/item"


def test_store_health_matrix_tracking():
    """Verifica el cálculo de latencias, estados de salud, conteo de glitches y circuit breaker."""
    tracker = StoreHealthTracker()
    tracker.register_store("falabella", "Falabella Chile")

    # Registro de escaneo rápido -> HEALTHY
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=120.0, items_count=25)
    matrix = tracker.get_matrix()
    store_rec = next(s for s in matrix if s["store_id"] == "falabella")
    assert store_rec["status"] == "HEALTHY"
    assert store_rec["latency_ms"] == 120.0
    assert store_rec["items_scanned"] == 25
    assert store_rec["avg_latency_ms"] == 120.0

    # Registro de glitch detectado
    tracker.record_glitch("falabella")
    matrix_after = tracker.get_matrix()
    store_rec_after = next(s for s in matrix_after if s["store_id"] == "falabella")
    assert store_rec_after["glitches_detected"] == 1

    # Registro de latencia alta (>4000ms) -> DEGRADED
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=5000.0, items_count=10)
    store_rec_deg = next(s for s in tracker.get_matrix() if s["store_id"] == "falabella")
    assert store_rec_deg["status"] == "DEGRADED"

    # Errores consecutivos -> Circuit Breaker (OFFLINE tras >=3 fallos)
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=10.0, items_count=0, error=True)
    assert next(s for s in tracker.get_matrix() if s["store_id"] == "falabella")["status"] == "DEGRADED"
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=10.0, items_count=0, error=True)
    assert next(s for s in tracker.get_matrix() if s["store_id"] == "falabella")["status"] == "DEGRADED"
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=10.0, items_count=0, error=True)
    assert next(s for s in tracker.get_matrix() if s["store_id"] == "falabella")["status"] == "OFFLINE"

    # Recuperación automática tras escaneo exitoso
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=150.0, items_count=30, error=False)
    store_rec_recovered = next(s for s in tracker.get_matrix() if s["store_id"] == "falabella")
    assert store_rec_recovered["status"] == "HEALTHY"
    assert store_rec_recovered["error_count"] == 0


def test_store_health_concurrency_and_rlock_safety():
    """Valida la seguridad concurrente de StoreHealthTracker con RLock y ausencia total de deadlocks."""
    import concurrent.futures
    tracker = StoreHealthTracker()
    num_threads = 10
    iterations_per_thread = 50

    def worker(worker_id: int):
        store_id = f"store_{worker_id % 3}"
        for i in range(iterations_per_thread):
            # Mutaciones concurrentes protegidas por RLock
            tracker.record_scan(
                store_id=store_id,
                store_name=f"Store {store_id}",
                latency_ms=100.0 + (i * 2),
                items_count=10,
                error=False,
            )
            if i % 5 == 0:
                tracker.record_glitch(store_id)
            # Lecturas concurrentes de matriz durante mutaciones intensivas
            matrix = tracker.get_matrix()
            assert isinstance(matrix, list)

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, tid) for tid in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # Si hubo deadlock o excepción, se propagará aquí

    matrix_final = tracker.get_matrix()
    assert len(matrix_final) == 3
    total_scans = sum(s["success_scans"] for s in matrix_final)
    assert total_scans == num_threads * iterations_per_thread
    total_items = sum(s["items_scanned"] for s in matrix_final)
    assert total_items == num_threads * iterations_per_thread * 10


def test_store_health_api_endpoint():
    """Valida el endpoint REST /api/store-health servido por FastAPI."""
    from fastapi.testclient import TestClient
    from glitchhunter_ai.web.server import app

    with TestClient(app) as client:
        res = client.get("/api/store-health")
        assert res.status_code == 200
        data = res.json()
        assert "stores" in data
        assert isinstance(data["stores"], list)


# =========================================================================
# 4. PRUEBAS DE DISCORD WEBHOOK Y WATCHLIST
# =========================================================================

def test_discord_alert_embed_generation(tmp_path):
    """Verifica la construcción del Embed enriquecido para Discord con colores y datos estructurados."""
    dispatcher = DiscordAlertDispatcher(webhook_url="https://discord.com/api/webhooks/mock/test", enabled=True)
    
    prod = ProductItem(
        id="test-discord-prod",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Apple MacBook Pro 16 M3 Max 36GB RAM",
        category="notebooks",
        current_price=249990.0,
        normal_price=2499990.0,
        card_price=239990.0,
        url="https://www.falabella.com/falabella-cl/product/16984210/MacBook-Pro-16",
        in_stock=True,
    )
    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=99.0,
        discount_percentage=90.0,
        absolute_savings_clp=2250000.0,
        is_missing_zero=True,
        semantic_check=SemanticClassificationResult(
            is_accessory=False,
            is_main_device=True,
            detected_item_type="notebook",
            explanation="Laptop de gama alta confirmado.",
            confidence=0.99
        ),
        reason="Missing Zero Bug masivo en MacBook Pro."
    )
    alert = GlitchAlert(
        alert_id="ALT-DISCORD-TEST",
        product=prod,
        verdict=verdict,
        alert_url=prod.url,
        add_to_cart_url="https://www.falabella.com/falabella-cl/checkout?skuId=16984210",
        is_watchlist_match=True,
    )

    embed = dispatcher.format_embed(alert)
    assert "WATCHLIST" in embed["title"]
    assert embed["color"] == 0xE74C3C  # Rojo crítico
    assert any(f["name"] == "🏪 Tienda" and "Falabella" in f["value"] for f in embed["fields"])
    assert any(f["name"] == "💳 Exclusivo Tarjeta" for f in embed["fields"])
    assert "1 Clic" in embed["description"]

    # Simulación de envío exitoso
    sent = dispatcher.send_alert(alert)
    assert sent is True


def test_watchlist_database_crud_and_matching(tmp_path):
    """Verifica operaciones CRUD de la Watchlist y detección de matches en el Pipeline."""
    db_file = tmp_path / "test_watchlist.db"
    db = PriceDatabase(db_path=db_file)

    # 1. Agregar palabras clave
    assert db.add_watchlist_keyword("MacBook") is True
    assert db.add_watchlist_keyword("RTX 4080") is True
    # Duplicado no genera error
    assert db.add_watchlist_keyword("macbook") is False

    keywords = db.get_watchlist_keywords()
    assert "macbook" in keywords
    assert "rtx 4080" in keywords

    # 2. Matching en títulos
    assert db.is_in_watchlist("Apple MacBook Pro 14 M3") is True
    assert db.is_in_watchlist("Tarjeta ASUS GeForce RTX 4080 Gaming") is True
    assert db.is_in_watchlist("Smart TV Samsung 55 Pulgadas") is False

    # 3. Eliminar palabra clave
    assert db.remove_watchlist_keyword("macbook") is True
    assert db.is_in_watchlist("Apple MacBook Pro 14 M3") is False
    assert len(db.get_watchlist_keywords()) == 1


def test_discord_severity_colors_and_edge_cases():
    """Valida mapeo exacto de colores de severidad y casos de borde en embeds de Discord."""
    dispatcher = DiscordAlertDispatcher(webhook_url="https://discord.com/api/webhooks/mock/test", enabled=True)

    prod = ProductItem(
        id="prod-color-test",
        store_id="ripley",
        store_name="Ripley Chile",
        title="PlayStation 5 Slim 1TB",
        category="consolas",
        current_price=299990.0,
        normal_price=549990.0,
        url="https://simple.ripley.cl/ps5-slim",
    )

    # 1. Probar todas las severidades
    severities = [
        (GlitchSeverity.CRITICO, 0xE74C3C),
        (GlitchSeverity.ALTO, 0xE67E22),
        (GlitchSeverity.MODERADO, 0x2ECC71),
        (GlitchSeverity.FALSO_POSITIVO, 0x95A5A6),
    ]

    for sev, expected_color in severities:
        verdict = GlitchVerdict(
            is_glitch=True,
            severity=sev,
            confidence_score=90.0,
            discount_percentage=45.0,
            absolute_savings_clp=250000.0,
            semantic_check=SemanticClassificationResult(
                is_accessory=False, is_main_device=True, detected_item_type="consola", explanation="Consola", confidence=0.9
            ),
            reason=f"Prueba severidad {sev.value}",
        )
        alert = GlitchAlert(
            alert_id=f"ALT-SEV-{sev.value}",
            product=prod,
            verdict=verdict,
            alert_url=prod.url,
            is_watchlist_match=False,
        )
        embed = dispatcher.format_embed(alert)
        assert embed["color"] == expected_color
        assert "🚨 [GLITCH DETECTADO]" in embed["title"]
        assert "⭐ [WATCHLIST HIT]" not in embed["title"]

    # 2. Despachador deshabilitado o sin URL debe retornar False
    disp_disabled = DiscordAlertDispatcher(webhook_url="https://discord.com/mock", enabled=False)
    assert disp_disabled.send_alert(alert) is False

    disp_no_url = DiscordAlertDispatcher(webhook_url=None, enabled=True)
    assert disp_no_url.send_alert(alert) is False


def test_discord_send_alert_http_responses_and_async():
    """Verifica respuestas HTTP (200, 204, 500, timeout) y despacho asíncrono."""
    import asyncio
    from unittest.mock import MagicMock, patch

    dispatcher = DiscordAlertDispatcher(webhook_url="https://discord.com/api/webhooks/real_id/token", enabled=True)

    prod = ProductItem(
        id="prod-net-test",
        store_id="paris",
        store_name="Paris Chile",
        title="Smart TV LG OLED 65 4K",
        category="televisores",
        current_price=199990.0,
        normal_price=1999990.0,
        url="https://www.paris.cl/lg-oled-65",
    )
    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=98.0,
        discount_percentage=90.0,
        absolute_savings_clp=1800000.0,
        semantic_check=SemanticClassificationResult(
            is_accessory=False, is_main_device=True, detected_item_type="tv", explanation="OLED TV", confidence=0.99
        ),
        reason="Missing Zero en OLED 65",
    )
    alert = GlitchAlert(alert_id="ALT-NET-1", product=prod, verdict=verdict, alert_url=prod.url)

    # Caso HTTP 204 (éxito estándar de Discord Webhook)
    mock_resp_204 = MagicMock()
    mock_resp_204.status_code = 204
    with patch("requests.post", return_value=mock_resp_204):
        assert dispatcher.send_alert(alert) is True

    # Caso HTTP 200 (éxito)
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    with patch("requests.post", return_value=mock_resp_200):
        assert dispatcher.send_alert(alert) is True

    # Caso HTTP 500 (error del servidor Discord)
    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500
    mock_resp_500.text = "Internal Server Error"
    with patch("requests.post", return_value=mock_resp_500):
        assert dispatcher.send_alert(alert) is False

    # Caso Excepción / Timeout de red
    with patch("requests.post", side_effect=Exception("Connection refused")):
        assert dispatcher.send_alert(alert) is False

    # Envío asíncrono
    with patch("requests.post", return_value=mock_resp_204):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            res_async = loop.run_until_complete(dispatcher.send_alert_async(alert))
            assert res_async is True
        finally:
            loop.close()


def test_pipeline_watchlist_boost_and_discord_dispatch(tmp_path):
    """Verifica que el Pipeline incremente la certeza (+10%) y despache a Discord al coincidir con la Watchlist."""
    from unittest.mock import MagicMock
    from glitchhunter_ai.notifications import NtfyAlertDispatcher

    db_file = tmp_path / "pipeline_wl_test.db"
    db = PriceDatabase(db_path=db_file)
    db.init_db()

    # Agregar 'rtx 4090' a la watchlist
    db.add_watchlist_keyword("RTX 4090")

    mock_discord = MagicMock(spec=DiscordAlertDispatcher)
    mock_discord.send_alert.return_value = True

    mock_ntfy = MagicMock(spec=NtfyAlertDispatcher)
    mock_ntfy.send_alert.return_value = True

    pipeline = GlitchHuntingPipeline(
        db=db,
        discord_dispatcher=mock_discord,
        ntfy_dispatcher=mock_ntfy,
    )

    # Producto que coincide con la watchlist
    wl_prod = ProductItem(
        id="gpu-001",
        store_id="pcfactory",
        store_name="PC Factory",
        title="Tarjeta de Video MSI GeForce RTX 4090 Suprim X 24GB",
        category="computacion",
        current_price=189990.0,
        normal_price=1899990.0,
        url="https://www.pcfactory.cl/producto/gpu-rtx4090",
        in_stock=True,
    )

    alert = pipeline.process_product(wl_prod)
    assert alert is not None
    assert alert.is_watchlist_match is True
    # Verificamos que se despachó a Discord
    mock_discord.send_alert.assert_called_once()
    dispatched_alert = mock_discord.send_alert.call_args[0][0]
    assert dispatched_alert.is_watchlist_match is True
    assert "RTX 4090" in dispatched_alert.product.title


def test_watchlist_api_endpoints():
    """Valida los endpoints REST /api/watchlist (GET, POST, DELETE) en FastAPI."""
    from fastapi.testclient import TestClient
    from glitchhunter_ai.web.server import app

    with TestClient(app) as client:
        test_kw = f"nintendo_switch_oled_{int(time.time())}"

        # POST para agregar
        post_res = client.post("/api/watchlist", json={"keyword": test_kw})
        assert post_res.status_code == 200
        post_data = post_res.json()
        assert post_data["success"] is True
        assert test_kw in post_data["keywords"]

        # GET para verificar persistencia
        get_res = client.get("/api/watchlist")
        assert get_res.status_code == 200
        get_data = get_res.json()
        assert test_kw in get_data["keywords"]

        # POST duplicado debe indicar success False
        dup_res = client.post("/api/watchlist", json={"keyword": test_kw})
        assert dup_res.status_code == 200
        assert dup_res.json()["success"] is False

        # DELETE para remover la palabra clave
        del_res = client.delete(f"/api/watchlist/{test_kw}")
        assert del_res.status_code == 200
        del_data = del_res.json()
        assert del_data["success"] is True
        assert test_kw not in del_data["keywords"]

        # DELETE de palabra inexistente
        del_nonexistent = client.delete("/api/watchlist/palabra_inexistente_xyz_123")
        assert del_nonexistent.status_code == 200
        assert del_nonexistent.json()["success"] is False



def test_solotodo_discount_strategy_uses_valid_ordering_and_recovers_from_400():
    """
    La API /products/browse/ de SoloTodo rechaza '-discount_percentage' con HTTP 400.
    La estrategia 'discount_desc' debe usar un orden válido, y un 400 por orden inválido
    debe reintentarse con el orden por defecto en vez de devolver 0 productos.
    """
    from unittest.mock import MagicMock
    from glitchhunter_ai.scrapers.solotodo import DEFAULT_ORDERING

    def _resp(status, results=None):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = {"results": results or []}
        return r

    entry = {"product_entries": [{
        "product": {"id": 1, "name": "Samsung Galaxy S25 Ultra 256GB", "slug": "s25-ultra"},
        # Precio en USD primero (tienda de otro país) y luego en CLP: solo debe usarse el CLP
        "metadata": {"prices_per_currency": [
            {"currency": 4, "currency_id": 4, "offer_price": "849.99", "normal_price": "849.99"},
            {"currency": 1, "currency_id": 1, "offer_price": "999990", "normal_price": "1469990"},
        ]},
    }]}

    scraper = SoloTodoScraper()
    scraper.client = MagicMock()
    scraper.client.get.return_value = _resp(200, [entry])
    items = scraper.fetch_catalog(use_live=True, active_category_ids=[6], sort_strategy="discount_desc")
    url = scraper.client.get.call_args[0][0]
    assert "-discount_percentage" not in url
    assert "ordering=leads" in url
    assert len(items) == 1
    assert (items[0].current_price, items[0].normal_price) == (999990.0, 1469990.0)

    # Producto sin precio en pesos chilenos (solo vendido en otros países): se omite
    foreign = {"product_entries": [{
        "product": {"id": 2, "name": "LG OLED55B5PSA", "slug": "lg"},
        "metadata": {"prices_per_currency": [{"currency_id": 4, "offer_price": "849.99", "normal_price": "849.99"}]},
    }]}
    scraper.client.get.return_value = _resp(200, [foreign])
    assert scraper._fetch_category_batch(11, 5, DEFAULT_ORDERING) == []

    # Si la API rechaza un orden, se reintenta con el orden por defecto
    scraper.client.get.reset_mock()
    scraper.client.get.side_effect = [_resp(400), _resp(200, [entry])]
    items = scraper._fetch_category_batch(6, 5, "orden-invalido")
    assert len(items) == 1
    assert f"ordering={DEFAULT_ORDERING}" in scraper.client.get.call_args_list[1][0][0]
