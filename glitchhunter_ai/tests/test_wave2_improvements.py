"""Pruebas unitarias para las mejoras detectadas por agentes Hydrogen + navegador (Wave 2)."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from glitchhunter_ai.core.best_deals import BestDealsCurator, score_post
from glitchhunter_ai.core.cart_sniper import generate_direct_add_to_cart_url
from glitchhunter_ai.core.categories import classify_category
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.scrapers.product_ids import canonical_product_id
from glitchhunter_ai.scrapers.social_radar import (
    SocialChannelRadar,
    extract_store_url_from_rata_link,
    parse_descuentos_rata_html,
)


def _tmp_db(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "wave2.db")
    db.init_db()
    return db


def test_paris_marketplace_cart_sniper_ignores_alphanumeric_skus():
    """SKUs alfanuméricos de Paris Marketplace (MK...) nunca generan cart-add?pid=0 ni pid=20."""
    mk_url = "https://www.paris.cl/audifonos-blik-air950-MKEDZOT0TK.html"
    mk2_url = "https://www.paris.cl/audifonos-lhotse-rm7-MKQMJAK2J0.html"
    assert generate_direct_add_to_cart_url("paris", mk_url, "paris-mkedzot0tk") == mk_url
    assert generate_direct_add_to_cart_url("paris", mk2_url, "paris-mkqmjak2j0") == mk2_url
    # Producto propio con SKU numérico real conserva el enlace directo al carrito
    own_url = "https://www.paris.cl/notebook-lenovo-883921999.html"
    assert generate_direct_add_to_cart_url("paris", own_url, "paris-883921999") == "https://www.paris.cl/cart-add?pid=883921999&quantity=1"


def test_category_classifier_disambiguates_detected_mismatches():
    """Corrige clasificaciones cruzadas vistas en producción."""
    assert classify_category("Bicicleta Spinning Profesional Q3 Pro 8 Kg + Monitor") == "deportes"
    assert classify_category("Procesador de Alimentos Professional 800W") == "hogar"
    assert classify_category("Vestido AE Midi con Volante") == "ropa_calzado"
    assert classify_category("Juego EA Sports FC 26 PS5") == "gaming"
    assert classify_category("Respaldo New Villarrica Single") == "hogar"


def test_best_deals_penalizes_flat_baseline_missing_zero_and_retail_bulk_and_dedupes_cross_store(tmp_path):
    """Falsos missing_zero sin caída 30d y carga_masiva retail no reciben bono de error verificado."""
    usb_fan = {
        "kind": "glitch",
        "status": "active",
        "best_price": 34990,
        "discount_pct": 91.0,
        "payload": {
            "product": {"title": "Ventilador Expandible USB 6W", "normal_price": 388780},
            "verdict": {
                "absolute_savings_clp": 353790,
                "is_missing_zero": True,
                "verified_drop": True,
                "baseline_30d": 34990.0,
                "evidence_source": "missing_zero_tech",
            },
        },
    }
    score_fan, reasons_fan = score_post(usb_fan)
    assert "Error de precio verificado" not in reasons_fan
    assert "Parece que le falta un cero" not in reasons_fan

    bulk_retail = {
        "kind": "glitch",
        "status": "active",
        "best_price": 39990,
        "discount_pct": 88.0,
        "payload": {
            "bulk_price_count": 14,
            "product": {"title": "Respaldo New Villarrica Single", "normal_price": 333250},
            "verdict": {
                "absolute_savings_clp": 293260,
                "is_missing_zero": False,
                "verified_drop": True,
                "baseline_30d": 39990.0,
                "evidence_source": "carga_masiva",
            },
        },
    }
    _, reasons_bulk = score_post(bulk_retail)
    assert "Error de precio verificado" not in reasons_bulk
    assert score_fan < 75.0

    # Deduplicación cross-store de un mismo producto (Monitor ThinkVision en Falabella y Sodimac)
    db = _tmp_db(tmp_path)
    for store_id in ("falabella", "sodimac"):
        pid = f"{store_id}-thinkvision"
        db.upsert_product(ProductItem(
            id=pid, store_id=store_id, store_name=store_id.title(),
            title='Monitor ThinkVision 3D 27" UHD IPS', category="monitores",
            current_price=199990, normal_price=499990, url=f"https://example.com/{pid}",
        ))
        db.publish_feed_offer(
            kind="glitch", product_id=pid, store_id=store_id, best_price=199990, discount_pct=60.0,
            payload={
                "product": {"title": 'Monitor ThinkVision 3D 27" UHD IPS', "normal_price": 499990},
                "verdict": {"verified_drop": True, "absolute_savings_clp": 300000},
            },
        )
    picks = BestDealsCurator(db).rank(days=2, limit=10)
    assert len(picks) == 1


def test_feed_diversifies_active_glitches_and_filters_verified_only(tmp_path, monkeypatch):
    """La carga inicial incluye glitches activos aunque haya cientos de deals más recientes."""
    import glitchhunter_ai.web.server as server

    db = _tmp_db(tmp_path)
    monkeypatch.setattr(server, "db", db)
    # 1 glitch antiguo pero activo + 30 deals posteriores
    db.upsert_product(ProductItem(
        id="paris-oled-g", store_id="paris", store_name="Paris",
        title="Smart TV LG OLED C4 55", category="televisores",
        current_price=299990, normal_price=1099990, url="https://www.paris.cl/tv-123456.html",
    ))
    db.publish_feed_offer(
        kind="glitch", product_id="paris-oled-g", store_id="paris", best_price=299990, discount_pct=72.7,
        payload={
            "product": {"id": "paris-oled-g", "title": "Smart TV LG OLED C4 55", "normal_price": 1099990},
            "verdict": {"verified_drop": True, "evidence_source": "historial_30d", "absolute_savings_clp": 800000},
        },
    )
    for i in range(30):
        pid = f"paris-deal-{i}"
        db.upsert_product(ProductItem(
            id=pid, store_id="paris", store_name="Paris",
            title=f"Polera Básica {i}", category="ropa_calzado",
            current_price=14990, normal_price=29990, url=f"https://www.paris.cl/polera-{100000 + i}.html",
        ))
        db.publish_feed_offer(
            kind="deal", product_id=pid, store_id="paris", best_price=14990, discount_pct=50.0,
            payload={"product_id": pid, "title": f"Polera Básica {i}", "normal_price": 29990, "current_price": 14990, "verified_drop": False},
        )

    client = TestClient(server.app)
    initial = client.get("/api/feed?limit=20").json()
    assert len(initial) == 20
    assert any(c["feed_kind"] == "glitch" and c["product"]["id"] == "paris-oled-g" for c in initial)

    verified = client.get("/api/feed?limit=20&verified_only=true").json()
    assert len(verified) == 1 and verified[0]["product"]["id"] == "paris-oled-g"


def test_social_radar_unwraps_affiliates_lider_ids_and_matches_normalized_titles(tmp_path):
    """Desenvuelve Soicos/Awin, genera ID canónico de Líder y mejora match en social_benchmark."""
    soicos_html = (
        '<a href="https://ad.soicos.com/-1uCY?dl=https%3A%2F%2Fwww.sodimac.cl%2Fsodimac-cl%2Farticulo%2F110317936%2FTaladro%2F110317936">Ir</a>'
    )
    assert extract_store_url_from_rata_link(soicos_html) == "https://www.sodimac.cl/sodimac-cl/articulo/110317936/Taladro/110317936"

    awin_link = "https://www.awin1.com/cread.php?awinmid=123&ued=https%3A%2F%2Fwww.lg.com%2Fcl%2Fmonitores%2F24u411b"
    assert extract_store_url_from_rata_link(f'<a href="{awin_link}">Ir</a>') == "https://www.lg.com/cl/monitores/24u411b"

    assert canonical_product_id("lider", "https://www.lider.cl/ip/telefonos/samsung-galaxy-tab-a11/2385029") == "lider-2385029"

    # Parseo limpia emojis/invisible chars e ignora precios USD
    rata_html = """
    <html><body>
      <a href="/oferta/90001/zapatillas"><h6>Paris</h6><small>5min</small>
        <img src="https://images.retail.cl/shoe.jpg" />
        <div class="font-weight-bold line-clamp--3">👟\u200d Zapatilla Urbana ML725BB Unisex - New Balance</div>
        <span class="rata-font-poppins">$49.990</span>
      </a>
      <a href="/oferta/90002/ps-game"><h6>PlayStation Store</h6><small>5min</small>
        <div class="font-weight-bold line-clamp--3">Juego PS5</div>
        <span class="rata-font-poppins">US$34</span>
      </a>
    </body></html>
    """
    parsed = parse_descuentos_rata_html(rata_html, now=datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc))
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Zapatilla Urbana ML725BB Unisex - New Balance"
    assert parsed[0]["image_url"] == "https://images.retail.cl/shoe.jpg"

    # Match por código de modelo + prefijo Paris + search_norm
    db = _tmp_db(tmp_path)
    early = (datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO products (id, store_id, title, category, url, current_price, normal_price, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            ("paris-357596", "paris", "Zapatilla Urbana ML725BB Unisex", "ropa_calzado", "https://www.paris.cl/zapatilla-357596.html", 49990, 99990, early),
        )
        conn.execute(
            "INSERT INTO price_history (product_id, price, timestamp) VALUES (?, ?, ?);",
            ("paris-357596", 49990, early),
        )
    bench = db.evaluate_social_deal(
        external_id="rata-90001",
        channel="descuentos_rata",
        store_id="paris",
        product_id="paris-357596005",
        title="Zapatilla Urbana ML725BB Unisex - New Balance",
        deal_price=49990.0,
        normal_price=99990.0,
        store_url="https://www.paris.cl/zapatilla-357596005.html",
        external_published_at="2026-11-17 15:00:00",
    )
    assert bench["outcome"] == "ahead"
    assert bench["product_id"] == "paris-357596"

    # Un producto sintético ingestado desde Descuentos Rata no debe auto-matchearse como 'behind' al re-evaluar
    later = (datetime(2026, 11, 17, 15, 1, tzinfo=timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO products (id, store_id, title, category, url, current_price, normal_price, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            ("bold-rata-90003", "bold", "Polera Jordan 85", "ropa_calzado", "https://bold.cl/p", 19990, 39990, later),
        )
        conn.execute(
            "INSERT INTO price_history (product_id, price, timestamp) VALUES (?, ?, ?);",
            ("bold-rata-90003", 19990, later),
        )
    bench_self = db.evaluate_social_deal(
        external_id="rata-90003",
        channel="descuentos_rata",
        store_id="bold",
        product_id="bold-rata-90003",
        title="Polera Jordan 85",
        deal_price=19990.0,
        normal_price=39990.0,
        store_url="https://bold.cl/p",
        external_published_at="2026-11-17 15:00:00",
    )
    assert bench_self["outcome"] == "captured"
