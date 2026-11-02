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
from glitchhunter_ai.scrapers.stealth_client import StealthHTTPClient, default_stealth_client
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


# =========================================================================
# 3. PRUEBAS DE MODO SNIPER (1-CLIC ADD-TO-CART) Y STORE HEALTH
# =========================================================================

def test_sniper_add_to_cart_url_generation():
    """Verifica la deducción y generación de links directos de compra para cada retail."""
    # Falabella
    fala_url = "https://www.falabella.com/falabella-cl/product/16912389/Smart-TV-Samsung-OLED/16912389"
    cart_fala = generate_direct_add_to_cart_url("falabella", fala_url)
    assert "checkout?skuId=16912389" in cart_fala

    # Ripley (VTEX)
    ripley_url = "https://simple.ripley.cl/lg-oled-65-2000390111p"
    cart_ripley = generate_direct_add_to_cart_url("ripley", ripley_url)
    assert "/checkout/cart/add?sku=2000390111" in cart_ripley

    # Líder
    lider_url = "https://www.lider.cl/catalogo/product/sku/1029381/lg-smart-tv-55"
    cart_lider = generate_direct_add_to_cart_url("lider", lider_url)
    assert "/cart/add?sku=1029381" in cart_lider

    # PC Factory
    pcf_url = "https://www.pcfactory.cl/producto/48912-nvidia-rtx4080super"
    cart_pcf = generate_direct_add_to_cart_url("pcfactory", pcf_url)
    assert "carro/agregar?id=48912" in cart_pcf


def test_store_health_matrix_tracking():
    """Verifica el cálculo de latencias, estados de salud y conteo de glitches."""
    tracker = StoreHealthTracker()
    tracker.register_store("falabella", "Falabella Chile")

    # Registro de escaneo rápido -> HEALTHY
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=120.0, items_count=25)
    matrix = tracker.get_matrix()
    store_rec = next(s for s in matrix if s["store_id"] == "falabella")
    assert store_rec["status"] == "HEALTHY"
    assert store_rec["latency_ms"] == 120.0
    assert store_rec["items_scanned"] == 25

    # Registro de glitch detectado
    tracker.record_glitch("falabella")
    matrix_after = tracker.get_matrix()
    store_rec_after = next(s for s in matrix_after if s["store_id"] == "falabella")
    assert store_rec_after["glitches_detected"] == 1

    # Registro de latencia alta -> DEGRADED
    tracker.record_scan("falabella", "Falabella Chile", latency_ms=5000.0, items_count=10)
    store_rec_deg = next(s for s in tracker.get_matrix() if s["store_id"] == "falabella")
    assert store_rec_deg["status"] == "DEGRADED"


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
