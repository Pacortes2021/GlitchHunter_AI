"""
Regresiones de los hallazgos del agente de QA (informe del 02-11-2026).
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from glitchhunter_ai.core.categories import classify_category
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.scrapers.pcfactory import PCFactoryScraper
from glitchhunter_ai.scrapers.product_ids import canonical_product_id


def _db(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "qa.db")
    db.init_db()
    return db


def _product(pid, title, price, normal, store="paris", **kw):
    return ProductItem(
        id=pid, store_id=store, store_name=store.title(), title=title, category=kw.pop("category", "tecnologia"),
        current_price=price, normal_price=normal, url=kw.pop("url", f"https://www.{store}.cl/{pid}.html"),
        in_stock=True, **kw,
    )


# --- Bug 1: ofertas 40-64% publicadas sin filtro de calidad ---
def test_deals_quality_gate_blocks_noise(tmp_path, monkeypatch):
    from glitchhunter_ai import config
    monkeypatch.setattr(config, "DEAL_INCLUDE_NON_TECH", False)  # solo categorías del radar
    db = _db(tmp_path)
    pipeline = GlitchHuntingPipeline(db=db)
    noise = [
        _product("p-zapatilla", "Zapatilla Running Duramo SL 2.0 Hombre", 23990, 59990),   # no es del radar
        _product("p-cama", "Cama 2 Plazas Turín con Doble Respaldo", 199990, 399990),       # no es del radar
        _product("p-ipad-reac", "Apple iPad A16 11 128GB WiFi (Reacondicionado)", 332990, 699990),
        _product("p-funda", "Funda Silicona iPhone 15 Pro", 9990, 19990),                    # accesorio
        _product("p-coby", "Parlante Bluetooth Coby CE1365", 990, 1990),                      # ahorra $1.000
    ]
    real = _product("p-tv", "Smart TV LG 55 UHD 55UR7800", 299990, 599990)
    for p in noise + [real]:
        db.upsert_product(p)

    assert pipeline.publish_new_deals() == 1
    assert [p["product_id"] for p in db.get_feed()] == ["p-tv"]


# --- Bug 2: ofertas que ya no aparecen en los escaneos no se publican ---
def test_stale_products_are_not_published(tmp_path):
    db = _db(tmp_path)
    pipeline = GlitchHuntingPipeline(db=db)
    old = _product("p-old", "Smart TV Samsung 65 QLED", 499990, 999990,
                   timestamp=datetime.now(timezone.utc) - timedelta(days=2))
    db.upsert_product(old)
    assert pipeline.publish_new_deals() == 0


# --- Bugs 3 y 4: IDs estables para Mercado Libre y PC Factory ---
def test_canonical_ids_mercadolibre_and_pcfactory():
    ad_click = ("https://click1.mercadolibre.cl/mclics/clicks/external/MLC/count?a=xyz"
                "&url=https%3A%2F%2Farticulo.mercadolibre.cl%2FMLC-1434557331-teclado&is_advertising=true")
    assert canonical_product_id("mercadolibre", ad_click) == "mercadolibre-mlc1434557331"
    assert canonical_product_id("mercadolibre", "https://www.mercadolibre.cl/p/MLC19176887") == "mercadolibre-mlc19176887"
    # El lector propio de PC Factory y SoloTodo producen el mismo ID
    assert canonical_product_id("pcfactory", "https://www.pcfactory.cl/producto/56531-ssd-kingston") == "pcfactory-56531"


def test_pcfactory_uses_credit_price_not_inflated_list_price():
    raw = [{
        "id": 56531, "nombre": "Unidad SSD 1TB", "marca": "Kingston",
        "precio": {"referencia": 399990, "normal": 278390, "efectivo": 269990},
        "stock": {"disponible": True},
    }]
    items = PCFactoryScraper().parse_pcfactory_json(raw)
    assert len(items) == 1
    item = items[0]
    assert item.id == "pcfactory-56531"
    assert item.normal_price == 278390      # antes 399.990 ('referencia') => falso -32%
    assert item.card_price == 269990
    assert item.discount_percentage < 5


# --- Bug 5: severidad guardada como 'GlitchSeverity.CRITICO' ---
def test_severity_is_stored_as_plain_value(tmp_path):
    db = _db(tmp_path)
    pipeline = GlitchHuntingPipeline(db=db)
    assert pipeline.process_product(_product("p-glitch", "Smart TV LG OLED 65", 64990, 1299990), send_notifications=False)
    assert db.get_recent_glitches()[0]["severity"] in ("CRITICO", "ALTO", "MODERADO")


# --- Bug 6: las simulaciones no contaminan el canal ---
def test_simulated_products_are_never_published(tmp_path):
    db = _db(tmp_path)
    pipeline = GlitchHuntingPipeline(db=db)
    alert = pipeline.process_product(
        _product("test-falabella-abc123", "Smart TV Samsung OLED 65 [TEST-ABC123]", 99990, 999990, store="falabella"),
        send_notifications=False,
    )
    assert alert is not None          # se procesa (para el WebSocket / la UI)...
    assert db.get_feed() == []        # ...pero no queda guardado en el canal


# --- Bug 12: una baja de $1 no es una oferta nueva ---
def test_republish_requires_meaningful_drop(tmp_path):
    db = _db(tmp_path)
    args = dict(kind="deal", product_id="p1", store_id="paris", discount_pct=50.0, payload={})
    assert db.publish_feed_offer(best_price=100000.0, **args)
    assert db.publish_feed_offer(best_price=99999.0, **args) is None     # -$1
    assert db.publish_feed_offer(best_price=99000.0, **args) is None     # -1%
    assert db.publish_feed_offer(best_price=97000.0, **args)             # -3% y -$3.000


# --- Bug 9: categoría por palabras completas ---
def test_category_uses_whole_words():
    assert classify_category("Zapatilla Running Duramo SL 2.0") == "otros"       # "ram" en Duramo
    assert classify_category("Set Ollas Tramontina 5 piezas") == "otros"
    assert classify_category("Memoria RAM DDR5 16GB") == "componentes"
    assert classify_category("Caja vacía iPhone 15 Pro") == "accesorios"
    assert classify_category("Notebook ASUS TUF RTX 4060") == "notebooks"


# --- Bugs 7, 14, 16 y menores: API ---
def test_api_csrf_watchlist_and_limits(tmp_path, monkeypatch):
    from glitchhunter_ai.web import server

    db = _db(tmp_path)
    monkeypatch.setattr(server, "db", db)
    client = TestClient(server.app)

    # CSRF: otro sitio no puede disparar acciones; el propio dashboard sí
    assert client.post("/api/test-glitch", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/watchlist", json={"keyword": "rtx 4090"},
                       headers={"Origin": "http://testserver"}).status_code == 200

    # Watchlist: largo 2-60 y borrado de palabras con '/'
    assert client.post("/api/watchlist", json={"keyword": "a"}).status_code == 422
    assert client.post("/api/watchlist", json={"keyword": "x" * 61}).status_code == 422
    assert client.post("/api/watchlist", json={"keyword": "rtx 4080/4090"}).status_code == 200
    resp = client.delete("/api/watchlist", params={"keyword": "rtx 4080/4090"})
    assert resp.status_code == 200 and resp.json()["success"] is True
    assert "rtx 4080/4090" not in resp.json()["keywords"]

    # Límites de parámetros
    assert client.get("/api/feed", params={"after_id": 10**30}).status_code == 422
    assert client.get("/api/deals", params={"min_discount": 70, "max_discount": 10}).status_code == 422


def test_watchlist_scan_respects_scan_lock(tmp_path):
    pipeline = GlitchHuntingPipeline(db=_db(tmp_path))
    pipeline.db.add_watchlist_keyword("macbook")
    pipeline._scan_lock.acquire()
    try:
        assert pipeline.run_watchlist_scan(use_live=False) == []
    finally:
        pipeline._scan_lock.release()


def test_legacy_unstable_ids_are_removed(tmp_path):
    """Las filas pcfactory_N (precio normal inflado) y MLC-HTML-… (hash inestable) se eliminan al iniciar."""
    db = _db(tmp_path)
    for pid, store in (("pcfactory_56531", "pcfactory"), ("MLC-HTML-1-186662", "mercadolibre"),
                       ("pcfactory-56531", "pcfactory"), ("mercadolibre-mlc123456", "mercadolibre")):
        db.upsert_product(_product(pid, "Unidad SSD 256GB SU650", 46390, 77990, store=store))
    assert db.remove_legacy_unstable_products() == 2
    with db.get_connection() as conn:
        ids = sorted(r["id"] for r in conn.execute("SELECT id FROM products"))
    assert ids == ["mercadolibre-mlc123456", "pcfactory-56531"]


def test_non_tech_deals_included_by_default_but_noise_still_blocked(tmp_path):
    """Por defecto se publican ofertas reales de cualquier categoría (la página filtra), pero no ruido."""
    db = _db(tmp_path)
    pipeline = GlitchHuntingPipeline(db=db)
    db.upsert_product(_product("p-parka", "Parka con Capucha Hombre", 22990, 44990))
    db.upsert_product(_product("p-reac", "Notebook HP 15 Reacondicionado", 299990, 699990))
    db.upsert_product(_product("p-funda", "Funda Silicona iPhone 15 Pro", 9990, 29990))
    pipeline.publish_new_deals()
    feed = db.get_feed()
    assert [p["product_id"] for p in feed] == ["p-parka"]
    assert feed[0]["payload"]["category"] == "otros"   # no "tecnologia" por defecto


def test_same_product_with_different_ids_is_published_once(tmp_path):
    """Mismo producto con IDs de distintas fuentes o formatos antiguos (ej: mercadolibre-kMLC… y mercadolibre-mlc…)."""
    db = _db(tmp_path)
    url = "https://www.mercadolibre.cl/microfono-bitbyte-m7/p/MLC52057898"
    common = dict(kind="deal", store_id="mercadolibre", best_price=28990.0, discount_pct=42.0)
    assert db.publish_feed_offer(product_id="mercadolibre-kMLC52057898", payload={"url": url}, **common)
    assert db.publish_feed_offer(product_id="mercadolibre-mlc52057898", payload={"url": url + "?tracking=x"}, **common) is None
    assert len(db.get_feed()) == 1


def test_frontend_is_always_revalidated():
    """Tras actualizar la app, el navegador no debe seguir mostrando la página antigua desde su caché."""
    from glitchhunter_ai.web import server

    client = TestClient(server.app)
    for path in ("/", "/static/app.js", "/static/styles.css"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-cache"
    assert "no-cache" not in (client.get("/api/status").headers.get("cache-control") or "")
