"""
Señales basadas en los casos históricos chilenos: nivel de riesgo de anulación, cifra de precio
inusual y modo evento (CyberDay, Black Friday, etc.).
"""

from datetime import datetime

from fastapi.testclient import TestClient

from glitchhunter_ai.core.price_signals import active_event, is_odd_price, parse_event_windows, risk_tier


def test_risk_tier_bands_from_court_cases():
    assert risk_tier(99.98) == "irrisorio"   # cama a $120: anulación validada por tribunales
    assert risk_tier(95.0) == "irrisorio"
    assert risk_tier(80.0) == "plausible"    # zona con casos respetados o compensados
    assert risk_tier(60.0) == "plausible"
    assert risk_tier(45.0) is None           # descuento comercial
    assert risk_tier(None) is None


def test_odd_price_endings():
    # Errores reales: cifras arbitrarias de porcentajes mal calculados
    assert is_odd_price(179991) and is_odd_price(29710) and is_odd_price(25872)
    # Precios fijados por el retail
    assert not is_odd_price(179990) and not is_odd_price(299990) and not is_odd_price(250000)
    # Precios bajos no cuentan (ruido)
    assert not is_odd_price(5872)
    # En marketplaces los vendedores fijan precios arbitrarios: no es señal
    assert not is_odd_price(104982, "mercadolibre")
    assert is_odd_price(104982, "falabella")


def test_event_windows_and_mode():
    windows = parse_event_windows("Black Friday=2026-11-26:2026-12-01, Navidad=2026-12-18:2026-12-24, roto=2026-13-40")
    assert [w.name for w in windows] == ["Black Friday", "Navidad"]   # la entrada inválida se ignora
    assert active_event(windows, "auto", datetime(2026, 11, 28, 23, 30)) == "Black Friday"
    assert active_event(windows, "auto", datetime(2026, 11, 10, 12, 0)) is None
    assert active_event(windows, "on", datetime(2026, 1, 1)) == "Modo evento manual"
    assert active_event(windows, "off", datetime(2026, 11, 28)) is None


def test_feed_posts_include_risk_and_odd_price(tmp_path, monkeypatch):
    from glitchhunter_ai.core.database import PriceDatabase
    from glitchhunter_ai.web import server

    db = PriceDatabase(db_path=tmp_path / "signals.db")
    db.init_db()
    monkeypatch.setattr(server, "db", db)
    base = {"store_id": "paris", "title": "x", "current_price": 0, "normal_price": 0}
    db.publish_feed_offer(kind="glitch", product_id="p1", store_id="paris", best_price=179991.0, discount_pct=80.0,
                          payload={"product": dict(base, id="p1", url="https://www.paris.cl/p1"),
                                   "verdict": {"discount_percentage": 80.0}})
    db.publish_feed_offer(kind="deal", product_id="p2", store_id="paris", best_price=29990.0, discount_pct=45.0,
                          payload=dict(base, product_id="p2", url="https://www.paris.cl/p2",
                                       current_price=29990, normal_price=54990, discount_pct=45.0))
    posts = {p["product"]["id"]: p for p in TestClient(server.app).get("/api/feed").json()}
    assert (posts["p1"]["risk_tier"], posts["p1"]["odd_price"]) == ("plausible", True)
    assert (posts["p2"]["risk_tier"], posts["p2"]["odd_price"]) == (None, False)

    status = TestClient(server.app).get("/api/status").json()
    assert "event_mode" in status and "direct_scan_interval_seconds" in status


# =====================================================================
# Carga masiva: muchos productos distintos al mismo precio
# =====================================================================
def _p(pid, title, price, normal, store="paris"):
    from glitchhunter_ai.core.models import ProductItem
    return ProductItem(id=pid, store_id=store, store_name=store, title=title, category="dormitorio",
                       current_price=price, normal_price=normal, url=f"https://www.{store}.cl/{pid}.html")


def test_detect_bulk_prices_cencosud_pattern():
    from glitchhunter_ai.core.price_signals import detect_bulk_prices

    # Caso real (Cencosud ~2020): camas y sets distintos, todos a $38.990
    bulk = [_p("a", "Cama Europea King", 38990, 739990), _p("b", "Set Dormitorio 2 plazas", 38990, 549990),
            _p("c", "Cama Rosen Super King", 38990, 899990), _p("d", "Respaldo + Velador", 38990, 199990)]
    # Variantes de color del mismo producto (mismo precio normal): no es carga masiva
    colors = [_p(f"c{i}", f"Galaxy S25 color {i}", 569990 // 2, 1069990, store="ripley") for i in range(5)]
    # Mismo precio en otra tienda no cuenta para el grupo de Paris
    other = [_p("x", "Otro", 38990, 500000, store="falabella")]
    res = detect_bulk_prices(bulk + colors + other)
    assert res == {"a": 4, "b": 4, "c": 4, "d": 4}
    # Con solo 3 productos no alcanza
    assert detect_bulk_prices(bulk[:3]) == {}


def test_bulk_price_is_independent_evidence_of_price_error(tmp_path):
    from unittest.mock import MagicMock
    from glitchhunter_ai.core.database import PriceDatabase
    from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline

    db = PriceDatabase(db_path=tmp_path / "bulk.db")
    db.init_db()
    reader = MagicMock()
    batch = [_p("paris-a", "Cama Europea King", 38990, 739990), _p("paris-b", "Set Dormitorio 2 plazas", 38990, 549990),
             _p("paris-c", "Cama Rosen Super King", 38990, 899990), _p("paris-d", "Respaldo Capitone + Velador", 38990, 199990)]
    reader.search.return_value = []
    reader.fetch_category.side_effect = lambda store, cat: batch if store == "paris" else []
    reader.fetch_catalog_slice.return_value = []
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=reader, confirm_with_store=False)

    pipeline.run_direct_scan(keywords=[], categories=["dormitorio"], send_notifications=False)
    posts = db.get_feed()
    # Sin historial ni referencia de mercado, el patrón de carga masiva basta para "error de precio"
    assert sorted(p["kind"] for p in posts) == ["glitch"] * 4
    assert all(p["payload"]["bulk_price_count"] == 4 for p in posts)
    assert "carga masiva" in posts[0]["payload"]["verdict"]["reason"]
