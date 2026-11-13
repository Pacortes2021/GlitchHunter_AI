"""
Pruebas de regresión y validación para el paquete de mejoras (detection, scrapers, DB, API y UI).
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from glitchhunter_ai.ai_detector.inflation_verifier import InflationVerifier
from glitchhunter_ai.ai_detector.math_detector import MathAnomalyDetector
from glitchhunter_ai.core.best_deals import BestDealsCurator
from glitchhunter_ai.core.catalog_rotator import CatalogRotator, CategoryDefinition
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.core.price_signals import detect_bulk_prices
from glitchhunter_ai.scrapers.direct_reader import (
    parse_shopify_products,
    parse_spdigital_product,
    parse_vtex_products,
)


def _tmp_db(tmp_path) -> PriceDatabase:
    return PriceDatabase(db_path=str(tmp_path / "overhaul.db"))


def test_skip_dotenv_isolation_in_subprocess(tmp_path):
    """Con GLITCHHUNTER_SKIP_DOTENV=1, un .env con overrides no pisa el entorno del proceso."""
    env_file = tmp_path / ".env"
    env_file.write_text("RADAR_ONLY_STORES=solo_esta_tienda\n", encoding="utf-8")

    env = dict(os.environ)
    env["GLITCHHUNTER_SKIP_DOTENV"] = "1"
    env.pop("RADAR_ONLY_STORES", None)
    code = (
        "import os; "
        "import glitchhunter_ai.config as cfg; "
        "print('|'.join(sorted(cfg.RADAR_ONLY_STORES)))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env={**env, "PYTHONPATH": os.getcwd()},
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == ""


def test_inflation_verifier_modified_z_score_and_history_stats():
    """Modified Z-Score MAD y resumen 30d/90d con detección de inflación previa a evento."""
    stable_series = [100000.0, 102000.0, 98000.0, 100000.0, 101000.0]
    z_drop = InflationVerifier.modified_z_score(19990.0, stable_series)
    assert z_drop is not None and z_drop < -5.0

    flat_series = [100000.0, 100000.0, 100000.0]
    z_flat = InflationVerifier.modified_z_score(40000.0, flat_series)
    assert z_flat is not None and z_flat < 0.0

    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    records = [
        {"price": 100000.0, "recorded_at": (now - timedelta(days=70)).isoformat()},
        {"price": 102000.0, "recorded_at": (now - timedelta(days=45)).isoformat()},
        {"price": 99000.0, "recorded_at": (now - timedelta(days=20)).isoformat()},
        {"price": 145000.0, "recorded_at": (now - timedelta(days=5)).isoformat()},
    ]
    stats = InflationVerifier.compute_history_stats(records, now=now)
    assert stats["median_90d"] == 101000.0
    assert stats["median_30d"] == 122000.0
    assert stats["min_90d"] == 99000.0
    assert stats["max_14d"] == 145000.0
    assert stats["days_span"] >= 60.0
    assert stats["pre_event_spike"] is True


def test_math_detector_revokes_missing_zero_when_baseline_contradicts():
    """Si el tachado es ~10x pero el baseline real difiere <20%, se revoca is_missing_zero."""
    detector = MathAnomalyDetector()
    product = ProductItem(
        id="test-contradict-01",
        store_id="falabella",
        store_name="Falabella",
        title="Monitor Gamer 27 Pulgadas QHD",
        category="monitores",
        current_price=50000.0,
        normal_price=500000.0,
        url="https://www.falabella.com/producto/123",
    )
    verdict = detector.evaluate(product, baseline_price=52000.0)
    assert verdict["details"]["baseline_contradiction"] is True
    assert verdict["is_missing_zero"] is False


def test_chronic_msrp_inflator_suppressed_and_excluded_from_bulk(tmp_path):
    """Tiendas con inflación crónica de MSRP requieren evidencia externa y se excluyen de bulk."""
    db = _tmp_db(tmp_path)
    now = datetime.now(timezone.utc)
    seed = [
        ProductItem(
            id=f"inflated-{i}",
            store_id="inflatedstore",
            store_name="Inflated Store",
            title=f"Chaqueta Urbana Modelo {i}",
            category="ropa",
            current_price=19990.0,
            normal_price=99990.0 + (i * 5000.0),
            url=f"https://inflated.example/p/{i}",
            timestamp=now,
        )
        for i in range(26)
    ]
    assert db.upsert_products_batch(seed) == 26

    stats = db.get_store_inflation_stats()
    assert stats["inflatedstore"]["is_chronic_inflator"] is True

    # Sin excluir, parecería caída masiva; al excluir la tienda inflada, desaparece el falso bulk.
    assert len(detect_bulk_prices(seed)) == 26
    assert detect_bulk_prices(seed, excluded_stores={"inflatedstore"}) == {}

    pipeline = GlitchHuntingPipeline(
        db=db,
        confirm_with_store=False,
    )
    candidate = ProductItem(
        id="real-check-inflated",
        store_id="inflatedstore",
        store_name="Inflated Store",
        title="Parka Térmica Edición Limitada",
        category="ropa",
        current_price=19990.0,
        normal_price=149990.0,
        url="https://inflated.example/p/parka",
        timestamp=now,
    )
    assert pipeline.process_product(candidate, send_notifications=False) is None


def test_direct_reader_spdigital_vtex_and_shopify_price_selection():
    """SP Digital ignora cuotas; VTEX y Shopify escogen el menor precio con stock válido."""
    sp_html = """
    <html><body>
      <h1>Notebook ASUS ROG Strix G16</h1>
      <div>12 cuotas de $12.990 sin interés (CAE 0%)</div>
      <div>$799.990</div>
      <div>$599.990</div>
      <div>$629.990</div>
    </body></html>
    """
    sp_item = parse_spdigital_product(
        sp_html, "https://www.spdigital.cl/notebook-asus-rog-strix-g16/"
    )
    assert sp_item is not None
    assert sp_item.current_price == 599990.0
    assert sp_item.normal_price == 799990.0

    vtex_raw = [
        {
            "productName": "Zapatilla Trail Pro",
            "link": "https://www.tiendavtex.cl/zapatilla-trail-pro/p",
            "items": [
                {
                    "unitMultiplier": 1,
                    "images": [{"imageUrl": "https://cdn.example/winner.jpg"}],
                    "sellers": [
                        {"commertialOffer": {"Price": 9990, "ListPrice": 99990, "AvailableQuantity": 0}},
                        {"commertialOffer": {"Price": 49990, "ListPrice": 89990, "AvailableQuantity": 4}},
                    ],
                },
                {
                    "unitMultiplier": 2,
                    "images": [{"imageUrl": "https://cdn.example/wrong-last-sku.jpg"}],
                    "sellers": [
                        {"commertialOffer": {"Price": 15000, "ListPrice": 89990, "AvailableQuantity": 0}},
                    ],
                },
            ],
        }
    ]
    vtex_items = parse_vtex_products("nike", vtex_raw)
    assert len(vtex_items) == 1
    assert vtex_items[0].current_price == 49990.0
    assert vtex_items[0].normal_price == 89990.0
    assert vtex_items[0].image_url == "https://cdn.example/winner.jpg"
    assert vtex_items[0].in_stock is True

    shopify_raw = [
        {
            "title": "Mochila Outdoor 30L",
            "handle": "mochila-outdoor-30l",
            "images": [{"src": "https://cdn.example/m.jpg"}],
            "variants": [
                {"price": "0", "compare_at_price": "59990", "available": True},
                {"price": "19990", "compare_at_price": "59990", "available": False},
                {"price": "39990", "compare_at_price": "59990", "available": True},
                {"price": "29990", "compare_at_price": "59990", "available": True},
            ],
        }
    ]
    shopify_items = parse_shopify_products("lippi", shopify_raw)
    assert len(shopify_items) == 1
    assert shopify_items[0].current_price == 29990.0
    assert shopify_items[0].normal_price == 59990.0


def test_rotator_persistence_watchlist_cap_and_rejected_cache_bound(tmp_path):
    """CatalogRotator persiste cursor en SQLite, watchlist respeta max_price y _rejected_deals es acotado."""
    db = _tmp_db(tmp_path)
    cats = [
        CategoryDefinition(key="notebooks", display_name="Notebooks", search_terms=["laptop"]),
        CategoryDefinition(key="monitores", display_name="Monitores", search_terms=["monitor"]),
        CategoryDefinition(key="ssd", display_name="SSD", search_terms=["nvme"]),
    ]

    r1 = CatalogRotator(categories=cats, batch_size=2, state_db=db)
    batch1 = r1.get_next_batch()
    assert [c.key for c in batch1.categories] == ["notebooks", "monitores"]

    # Una nueva instancia leyendo la misma BD retoma desde índice 2 sin reiniciar
    r2 = CatalogRotator(categories=cats, batch_size=2, state_db=db)
    batch2 = r2.get_next_batch()
    assert [c.key for c in batch2.categories] == ["ssd", "notebooks"]

    assert db.add_watchlist_keyword("iphone 16", max_price=500000.0) is True
    entries = db.get_watchlist_entries()
    assert any(e["keyword"] == "iphone 16" and e["max_price"] == 500000.0 for e in entries)
    assert db.is_in_watchlist("Apple iPhone 16 Pro 256GB", price=450000.0) is True
    assert db.is_in_watchlist("Apple iPhone 16 Pro 256GB", price=650000.0) is False

    pipeline = GlitchHuntingPipeline(db=db, confirm_with_store=False)
    pipeline._rejected_deals_max = 5
    now = datetime.now(timezone.utc)
    accessories = [
        ProductItem(
            id=f"acc-{idx}",
            store_id="pcfactory",
            store_name="PC Factory",
            title=f"Carcasa Funda Silicona para iPhone Modelo {idx}",
            category="accesorios",
            current_price=19990.0,
            normal_price=79990.0,
            url=f"https://www.pcfactory.cl/producto/{100 + idx}",
            timestamp=now,
        )
        for idx in range(7)
    ]
    assert db.upsert_products_batch(accessories) == 7
    assert pipeline.publish_new_deals() == 0
    assert len(pipeline._rejected_deals) == 5


def test_web_api_history_best_votes_watchlist_cap_and_frontend_assets(tmp_path, monkeypatch):
    """Valida /api/products/{id}/history, votos en /api/best, max_price en /api/watchlist y UI."""
    from glitchhunter_ai.web import server

    db = _tmp_db(tmp_path)
    monkeypatch.setattr(server, "db", db)
    monkeypatch.setattr(server, "best_deals", BestDealsCurator(db))
    monkeypatch.setattr(server, "API_AUTH_KEY", "")

    now = datetime.now(timezone.utc)
    for days_ago, price in [(10, 499990.0), (5, 479990.0), (0, 249990.0)]:
        db.upsert_product(
            ProductItem(
                id="tv-lg-c4",
                store_id="paris",
                store_name="Paris",
                title="Smart TV LG OLED C4 55",
                category="televisores",
                current_price=price,
                normal_price=799990.0,
                url="https://www.paris.cl/tv-lg-c4",
                timestamp=now - timedelta(days=days_ago),
            )
        )

    client = TestClient(server.app)

    hist_resp = client.get("/api/products/tv-lg-c4/history?days=90")
    assert hist_resp.status_code == 200
    hist = hist_resp.json()
    assert hist["product_id"] == "tv-lg-c4"
    assert hist["min_90d"] == 249990.0
    assert len(hist["points"]) >= 3
    assert client.get("/api/products/no-existe-xyz/history").status_code == 404

    payload = {
        "alert_id": "tv-lg-c4",
        "origin": "EN_VIVO",
        "product": {
            "id": "tv-lg-c4",
            "store_id": "paris",
            "store_name": "Paris",
            "title": "Smart TV LG OLED C4 55",
            "category": "televisores",
            "current_price": 249990.0,
            "normal_price": 799990.0,
            "url": "https://www.paris.cl/tv-lg-c4",
        },
        "verdict": {
            "is_glitch": True,
            "severity": "CRITICO",
            "confidence_score": 92.0,
            "discount_percentage": 68.75,
            "absolute_savings_clp": 550000.0,
            "verified_drop": True,
        },
    }
    feed_id = db.publish_feed_offer(
        kind="glitch",
        product_id="tv-lg-c4",
        store_id="paris",
        best_price=249990.0,
        discount_pct=68.75,
        payload=payload,
    )
    assert feed_id is not None

    assert client.post("/api/deals/tv-lg-c4/feedback", json={"vote": "up"}).status_code == 200
    best_up = client.get("/api/best?days=7")
    assert best_up.status_code == 200
    cards_up = best_up.json()
    assert len(cards_up) == 1
    assert cards_up[0]["upvotes"] == 1
    assert cards_up[0]["downvotes"] == 0

    assert client.post(
        "/api/deals/tv-lg-c4/feedback",
        json={"vote": "down", "reason": "fake_discount"},
    ).status_code == 200
    best_down = client.get("/api/best?days=7")
    assert best_down.status_code == 200
    cards_down = best_down.json()
    assert len(cards_down) == 1
    assert cards_down[0]["upvotes"] == 0
    assert cards_down[0]["downvotes"] == 1

    wl_resp = client.post(
        "/api/watchlist",
        json={"keyword": "oled lg", "max_price": 600000},
        headers={"Origin": "http://testserver"},
    )
    assert wl_resp.status_code == 200
    wl_data = wl_resp.json()
    assert "oled lg" in wl_data["keywords"]
    assert any(e["keyword"] == "oled lg" and e["max_price"] == 600000.0 for e in wl_data["entries"])

    index_html = client.get("/").text
    app_js = client.get("/static/app.js").text
    styles_css = client.get("/static/styles.css").text
    assert "filter-verified" in index_html
    assert "btn-toggle-admin" in index_html
    assert "watchlist-max-price" in index_html
    assert "loadStoreDirectory" in app_js
    assert "sparkline-svg" in app_js
    assert "handleCardInteraction" in app_js
    assert ".sparkline-svg" in styles_css


def test_knasta_403_triggers_cooldown_circuit_breaker():
    """Ante HTTP 403/429 de Knasta, se pausa el host sin bombardear las consultas siguientes."""
    from unittest.mock import MagicMock

    from glitchhunter_ai.scrapers import live_feed

    live_feed._knasta_cache.clear()
    try:
        feed = live_feed.LiveRetailFeed()
        resp_403 = MagicMock()
        resp_403.status_code = 403
        feed.session = MagicMock()
        feed.session.get.return_value = resp_403

        assert feed.search_live_products("iphone 16") == []
        first_urls = [c[0][0] for c in feed.session.get.call_args_list]
        assert sum(1 for u in first_urls if "knasta.cl" in u) == 1
        assert sum(1 for u in first_urls if "solotodo.com" in u) == 1

        # Segunda búsqueda dentro de la ventana de enfriamiento no vuelve a golpear HTTP 403
        # y activa el respaldo automático de SoloTodo para no dejar ciega la Watchlist.
        st_browse = MagicMock()
        st_browse.status_code = 200
        st_browse.json.return_value = {
            "results": [
                {
                    "picture_url": "https://cdn.example/rtx5070.jpg",
                    "product_entries": [
                        {"product": {"id": 9901, "name": "Tarjeta de Video RTX 5070 12GB"}}
                    ],
                }
            ]
        }
        st_entities = MagicMock()
        st_entities.status_code = 200
        st_entities.json.return_value = [
            {
                "id": 777,
                "store_id": 12,
                "currency_id": 1,
                "condition": "https://schema.org/NewCondition",
                "external_url": "https://www.pcfactory.cl/producto/55123-rtx-5070",
                "active_registry": {"is_available": True, "offer_price": 499990, "normal_price": 749990},
            }
        ]
        feed.session.get.side_effect = [st_browse, st_entities]
        fallback_items = feed.search_live_products("rtx 5070")
        assert len(fallback_items) == 1
        assert fallback_items[0].store_id == "pcfactory"
        assert fallback_items[0].source == "solotodo"
        assert fallback_items[0].current_price == 499990.0
        # Knasta NO fue golpeado otra vez (solo los 2 calls de SoloTodo: browse + entities)
        called_urls = [c[0][0] for c in feed.session.get.call_args_list]
        assert sum(1 for u in called_urls if "knasta.cl" in u) == 1
    finally:
        live_feed._knasta_cache.clear()


def test_prune_old_price_history_preserves_latest_per_product(tmp_path):
    """Poda registros antiguos >120d sin borrar el último precio de respaldo de productos inactivos."""
    db = _tmp_db(tmp_path)
    now = datetime.now(timezone.utc)
    old_ts = now - timedelta(days=150)
    recent_ts = now - timedelta(days=5)

    # Producto A: tiene muestra vieja (150d) y reciente (5d) -> se poda solo la vieja
    db.upsert_product(
        ProductItem(
            id="prod-a",
            store_id="pcfactory",
            store_name="PC Factory",
            title="Monitor 27 IPS",
            category="monitores",
            current_price=199990.0,
            normal_price=249990.0,
            url="https://www.pcfactory.cl/producto/1001",
            timestamp=old_ts,
        )
    )
    db.upsert_product(
        ProductItem(
            id="prod-a",
            store_id="pcfactory",
            store_name="PC Factory",
            title="Monitor 27 IPS",
            category="monitores",
            current_price=149990.0,
            normal_price=249990.0,
            url="https://www.pcfactory.cl/producto/1001",
            timestamp=recent_ts,
        )
    )
    # Producto B: solo tiene una muestra vieja (150d) -> se conserva como última referencia
    db.upsert_product(
        ProductItem(
            id="prod-b",
            store_id="pcfactory",
            store_name="PC Factory",
            title="Teclado Mecánico",
            category="otros",
            current_price=49990.0,
            normal_price=69990.0,
            url="https://www.pcfactory.cl/producto/1002",
            timestamp=old_ts,
        )
    )

    assert len(db.get_price_history("prod-a")) == 2
    assert len(db.get_price_history("prod-b")) == 1

    deleted = db.prune_old_price_history(keep_days=120, batch_size=100)
    assert deleted == 1
    assert len(db.get_price_history("prod-a")) == 1
    assert len(db.get_price_history("prod-b")) == 1
