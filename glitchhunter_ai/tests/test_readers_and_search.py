"""
Tests de los lectores por tienda (ritmo propio y carriles rápido/recorrido), del carril rápido de
Shopify y del buscador (catálogo guardado y búsqueda en vivo en las tiendas).
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.live_search import LiveSearch, matches_query
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.core.scheduler import DEEP, HOT, StoreScheduler, parse_interval_overrides
from glitchhunter_ai.scrapers.direct_reader import (
    HOT_LANE_STORES,
    SHOPIFY_STORES,
    DirectStoreReader,
    StoreBackoff,
    magento_has_sale_section,
    pick_shopify_sale_collection,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _product(pid, store="sony", price=50000, normal=100000, title="Audífonos Sony WH-1000XM5"):
    return ProductItem(id=pid, store_id=store, store_name=store, title=title, category="audio",
                       current_price=price, normal_price=normal, url=f"https://x.cl/{pid}", source="direct")


# ---------------------------------------------------------------- planificador
def test_scheduler_hot_lane_fixed_and_deep_lane_adapts():
    clock = _Clock()
    sched = StoreScheduler(hot_interval=60, deep_interval=180, deep_min=60, deep_max=900, clock=clock)
    sched.add("gap", HOT)
    sched.add("gap", DEEP)
    clock.t += 31   # las primeras lecturas se reparten dentro de los primeros 30 s
    assert set(sched.due()) == {("gap", HOT), ("gap", DEEP)}
    assert sched.due() == []          # en lectura: no se vuelven a entregar
    sched.done("gap", DEEP, count=100, fresh=5)
    sched.done("gap", HOT, count=50, fresh=0)
    lanes = {(r["store_id"], r["lane"]): r for r in sched.snapshot()}
    assert lanes[("gap", DEEP)]["interval_s"] == 90      # trajo ofertas nuevas: la mitad
    assert lanes[("gap", HOT)]["interval_s"] == 60       # el carril rápido no cambia
    clock.t += 61
    assert sched.due() == [("gap", HOT)]
    sched.done("gap", HOT, 50, 0)
    clock.t += 30
    assert sched.due() == [("gap", DEEP)]                 # 91 s después del inicio de su lectura
    sched.done("gap", DEEP, count=0, fresh=0, ok=False)   # no respondió: el doble
    assert {(r["lane"]): r["interval_s"] for r in sched.snapshot()}[DEEP] == 180


def test_scheduler_deep_interval_bounds_and_speed():
    clock = _Clock()
    sched = StoreScheduler(hot_interval=60, deep_interval=180, deep_min=60, deep_max=900, clock=clock)
    sched.add("weplay", DEEP)
    for _ in range(6):
        clock.t += 1000
        sched.due()
        sched.done("weplay", DEEP, count=40, fresh=3)
    assert sched.lanes[("weplay", DEEP)].interval == 60
    for _ in range(8):
        clock.t += 1000
        sched.due()
        sched.done("weplay", DEEP, count=0, fresh=0)
    assert sched.lanes[("weplay", DEEP)].interval == 900
    sched.speed = 0.5   # modo evento
    clock.t += 1000
    sched.due()
    sched.done("weplay", DEEP, count=0, fresh=0)
    assert sched.lanes[("weplay", DEEP)].next_at == clock.t + 450


def test_parse_interval_overrides():
    assert parse_interval_overrides("gap=60, falabella = 90,bad,x=-1,y=abc") == {"gap": 60.0, "falabella": 90.0}


# ---------------------------------------------------------------- carriles del lector
def _vtex_response(products, total, status=206):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = products
    resp.headers = {"resources": f"0-49/{total}"}
    return resp


def test_vtex_hot_and_deep_lanes_are_separate():
    products = json.loads((FIXTURES / "vtex_sony.json").read_text(encoding="utf-8"))
    client = MagicMock()
    client.get.return_value = _vtex_response(products, total=120)
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_Clock()))
    assert len(reader.fetch_hot("sony")) == 3
    assert [c.kwargs["params"]["O"] for c in client.get.call_args_list] == ["OrderByBestDiscountDESC"]
    client.get.reset_mock()
    reader.fetch_deep("sony", pages=2)
    assert [c.kwargs["params"]["O"] for c in client.get.call_args_list] == ["OrderByReleaseDateDESC"] * 2


def test_magento_hot_lane_only_for_sale_sections():
    assert magento_has_sale_section("dbs") and magento_has_sale_section("weplay")
    assert not magento_has_sale_section("thenorthface")
    assert "dbs" in HOT_LANE_STORES and "thenorthface" not in HOT_LANE_STORES and "sony" in HOT_LANE_STORES
    client = MagicMock()
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_Clock()))
    assert reader.fetch_hot("thenorthface") == []
    client.get.assert_not_called()


def test_pick_shopify_sale_collection():
    cols = [
        {"handle": "hombre", "title": "Hombre", "products_count": 300},
        {"handle": "cyber-day", "title": "Cyber Day", "products_count": 0},
        {"handle": "zapatillas-en-oferta", "title": "Zapatillas en oferta", "products_count": 40},
        {"handle": "sale", "title": "Sale", "products_count": 12},
    ]
    assert pick_shopify_sale_collection(cols) == "sale"                  # nombre exacto primero
    assert pick_shopify_sale_collection(cols[:3]) == "zapatillas-en-oferta"
    assert pick_shopify_sale_collection(cols[:2]) is None                 # vacía o sin coincidencia


def test_shopify_turns_alternate_sale_collection_and_catalog():
    products = json.loads((FIXTURES / "shopify_columbia.json").read_text(encoding="utf-8"))["products"]

    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/collections.json"):
            resp.json.return_value = {"collections": [{"handle": "outlet", "title": "Outlet", "products_count": 9}]}
        else:
            resp.json.return_value = {"products": products}
        return resp

    client = MagicMock()
    client.get.side_effect = fake_get
    clock = _Clock()
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=clock))
    reader.clock = clock
    first = list(SHOPIFY_STORES)[0]

    store, lane, items = reader.fetch_shopify_next()          # 1) carril rápido: descubre la colección
    assert (store, lane, items) == (first, "hot", []) and reader._shopify_sale[first] == "outlet"
    assert reader.fetch_shopify_next() is None                  # respeta el espaciado global
    clock.t += 91
    store, lane, items = reader.fetch_shopify_next()          # 2) recorrido del catálogo
    assert (store, lane, len(items)) == (first, "deep", 3)
    assert client.get.call_args.args[0].endswith("/products.json")
    clock.t += 91
    reader.fetch_shopify_next()                                # 3) descubre la siguiente tienda
    for s in SHOPIFY_STORES:
        reader._shopify_sale.setdefault(s, None)
    reader._shopify_hot_turn = 0
    clock.t += 91
    reader.fetch_shopify_next()                                # 4) recorrido
    clock.t += 91
    store, lane, items = reader.fetch_shopify_next()          # 5) colección de ofertas de la primera
    assert (store, lane, len(items)) == (first, "hot", 3)
    assert client.get.call_args.args[0].endswith("/collections/outlet/products.json")


# ---------------------------------------------------------------- ofertas frescas y procesamiento por tienda
def test_count_fresh_deals_and_process_store_products(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "r.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    pipeline.telegram_dispatcher.send_alert = MagicMock(return_value=True)
    a = _product("sony-a", price=50000)
    b = _product("sony-b", price=90000)       # 10%: no es oferta
    alerts, fresh = pipeline.process_store_products("sony", [a, b, a], send_notifications=False)
    assert fresh == 1
    assert pipeline.count_fresh_deals([a]) == 0                             # ya conocido al mismo precio
    assert pipeline.count_fresh_deals([_product("sony-a", price=45000)]) == 1   # bajó de precio
    assert not pipeline._scan_lock.locked()


# ---------------------------------------------------------------- buscador
def test_search_products_ignores_accents_and_case(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "s.db")
    db.init_db()
    db.upsert_product(_product("x-1", title="Cafetera Oster Café Expreso", price=40000, normal=80000))
    db.upsert_product(_product("x-2", title="Cafe molido 250g", price=5000, normal=5000))
    db.upsert_product(_product("x-3", title="Hervidor eléctrico", price=10000, normal=20000))
    rows = db.search_products("CAFÉ")
    assert [r["product_id"] for r in rows] == ["x-1", "x-2"]               # ordenado por descuento
    assert rows[0]["discount_pct"] == 50.0 and rows[0]["best_price"] == 40000
    assert [r["product_id"] for r in db.search_products("cafe", only_discounted=True)] == ["x-1"]
    assert db.search_products("electrico oster") == []                      # todas las palabras
    assert db.search_products(" ") == []


def test_matches_query_requires_half_the_words():
    assert matches_query("Audífonos Sony WH-1000XM5", ["audifonos", "bluetooth"])
    assert not matches_query("Parlante JBL", ["audifonos", "bluetooth"])
    assert not matches_query("Audífonos Sony", ["audifonos", "bluetooth", "jbl"])


def test_live_search_runs_all_stores_and_saves_results(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "l.db")
    db.init_db()
    reader = MagicMock()
    reader.search.side_effect = lambda store, q: (
        [_product(f"{store}-1", store=store, title="Audífonos Sony")] if store in ("sony", "falabella") else []
    )
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=reader)
    pipeline.telegram_dispatcher.send_alert = MagicMock(return_value=True)
    search = LiveSearch(pipeline, workers=4)
    job = search.start("  audífonos   ")
    assert search.start("AUDIFONOS")["id"] == job["id"]          # misma búsqueda: se reutiliza
    for _ in range(100):
        snap = search.snapshot(job["id"])
        if snap["finished"]:
            break
        time.sleep(0.05)
    assert snap["finished"] and snap["done_stores"] == snap["total_stores"] == len(search.stores)
    assert {r["product_id"] for r in snap["results"]} == {"sony-1", "falabella-1"}
    assert snap["results"][0]["source"] == "live" and snap["results"][0]["discount_pct"] == 50.0
    for _ in range(100):   # lo encontrado queda guardado en el catálogo
        if db.get_product("sony-1"):
            break
        time.sleep(0.05)
    assert db.get_product("sony-1") is not None


def test_search_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    import glitchhunter_ai.web.server as server

    client = TestClient(server.app)
    monkeypatch.setattr(server.db, "search_products", MagicMock(return_value=[
        {"product_id": "sony-a", "store_id": "sony", "title": "Audífonos", "best_price": 1, "discount_pct": 10}
    ]))
    res = client.get("/api/search", params={"q": "audifonos"})
    assert res.status_code == 200 and res.json()["results"][0]["source"] == "radar"
    assert client.get("/api/search", params={"q": "a"}).status_code == 422
    monkeypatch.setattr(server.live_search, "start", MagicMock(return_value=None))
    assert client.post("/api/search/live", json={"q": "audifonos"}).status_code == 429
    assert client.get("/api/search/live/nope").status_code == 404
    assert client.get("/api/readers").json()["active"] in (True, False)


def test_search_without_results_does_not_pause_store():
    def page(text, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        return resp

    client = MagicMock()
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_Clock()))
    client.get.return_value = page("<html><p>Tu búsqueda no obtuvo resultados.</p></html>")
    assert reader.search("gap", "freidora") == []
    assert reader.search("falabella", "zzzz") == []
    assert not reader.backoff.is_paused("gap") and not reader.backoff.is_paused("falabella")
    # Una verificación anti-bot o un 403 sí pausan la tienda
    client.get.return_value = page("<html>Please complete the captcha</html>")
    reader.search("gap", "freidora")
    assert reader.backoff.is_paused("gap")
    client.get.return_value = page("", status=403)
    reader.search("falabella", "tv")
    assert reader.backoff.is_paused("falabella")


# ---------------------------------------------------------------- alertas sin descuento
def test_magento_view_urls_use_product_number_not_category():
    from glitchhunter_ai.scrapers.product_ids import canonical_product_id

    a = "https://volcom.cl/catalog/product/view/id/472043/s/hombre-calzado-zapatillas-gris-7z210/category/4742/"
    b = "https://volcom.cl/catalog/product/view/id/519395/s/hombre-accesorios-calcetines/category/4742/"
    assert canonical_product_id("volcom", a) == "volcom-472043"
    assert canonical_product_id("volcom", b) == "volcom-519395"
    assert canonical_product_id("volcom", "https://volcom.cl/zapatillas-fusion.html") == "volcom-zapatillas-fusion"


def test_drop_only_vs_history_reports_history_discount():
    from glitchhunter_ai.ai_detector.math_detector import MathAnomalyDetector

    item = _product("marsella-x", store="marsella", price=85944, normal=85944, title="Equilibrador de carga A5298")
    ev = MathAnomalyDetector().evaluate(item, baseline_price=447992)
    assert ev["is_glitch_candidate"] and ev["reference_is_baseline"]
    assert round(ev["discount_percentage"]) == 81 and ev["reference_price"] == 447992
    # Con rebaja visible en la tienda la referencia sigue siendo el precio tachado
    ev2 = MathAnomalyDetector().evaluate(_product("m-2", price=20000, normal=100000), baseline_price=100000)
    assert not ev2["reference_is_baseline"] and ev2["discount_percentage"] == 80


def test_no_price_error_alert_with_zero_discount(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "z.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock(), confirm_with_store=False)
    pipeline.telegram_dispatcher.send_alert = MagicMock(return_value=True)
    pipeline.math_detector.evaluate = MagicMock(return_value={
        "is_glitch_candidate": True, "is_suspicious": True, "is_math_glitch": True, "is_missing_zero": False,
        "discount_percentage": 0.0, "absolute_savings_clp": 0.0, "confidence_score": 60.0,
    })
    item = _product("volcom-1", store="volcom", price=14990, normal=14990, title="Calcetines Full Stone 3 pack")
    assert pipeline.process_product(item, send_notifications=False) is None
    assert db.get_feed(limit=10) == []


def test_store_split_between_radars(monkeypatch):
    """RADAR_ONLY_STORES / RADAR_SKIP_STORES reparten las tiendas entre dos radares sin solaparse."""
    from glitchhunter_ai import config
    from glitchhunter_ai.core.continuous_reader import SHOPIFY_KEY, ContinuousReader

    blocked = {"ripley", "paris", "sodimac", list(SHOPIFY_STORES)[0]}
    pipeline = MagicMock()
    pipeline.direct_reader = DirectStoreReader(client=MagicMock())

    monkeypatch.setattr(config, "RADAR_ONLY_STORES", set())
    monkeypatch.setattr(config, "RADAR_SKIP_STORES", blocked)
    server = ContinuousReader(pipeline, workers=2, hot_interval=60, deep_interval=180, deep_min=60, deep_max=900)
    server_stores = {s for (s, _) in server.scheduler.lanes} | set(pipeline.direct_reader.shopify_stores)
    assert not (server_stores & blocked)
    assert (SHOPIFY_KEY, "turno") in server.scheduler.lanes
    server.pool.shutdown(wait=False)

    monkeypatch.setattr(config, "RADAR_ONLY_STORES", blocked)
    monkeypatch.setattr(config, "RADAR_SKIP_STORES", set())
    home_reader = DirectStoreReader(client=MagicMock())
    pipeline.direct_reader = home_reader
    home = ContinuousReader(pipeline, workers=2, hot_interval=60, deep_interval=180, deep_min=60, deep_max=900)
    home_stores = {s for (s, _) in home.scheduler.lanes if s != SHOPIFY_KEY} | set(home_reader.shopify_stores)
    assert home_stores == blocked
    assert home_reader.shopify_stores == [list(SHOPIFY_STORES)[0]]
    home.pool.shutdown(wait=False)

    live = LiveSearch(pipeline, workers=2)
    assert set(live.stores) <= blocked and "ripley" in live.stores


def test_shopify_rotation_with_no_enabled_shops_does_nothing():
    reader = DirectStoreReader(client=MagicMock())
    reader.shopify_stores = []
    assert reader.fetch_shopify_next() is None
    reader.client.get.assert_not_called()
