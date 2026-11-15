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
    BIG_HOT_MAX_PAGES,
    CATEGORY_MAX_PAGES,
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


def test_shopify_alias_covers_all_shopify_stores(monkeypatch):
    from glitchhunter_ai import config
    first = list(SHOPIFY_STORES)[0]
    monkeypatch.setattr(config, "RADAR_ONLY_STORES", set())
    monkeypatch.setattr(config, "RADAR_SKIP_STORES", {"shopify", "ripley"})
    assert not config.store_enabled(first) and not config.store_enabled("ripley")
    assert config.store_enabled("pcfactory")
    monkeypatch.setattr(config, "RADAR_ONLY_STORES", {"shopify", "ripley"})
    monkeypatch.setattr(config, "RADAR_SKIP_STORES", set())
    assert config.store_enabled(first) and config.store_enabled("ripley")
    assert not config.store_enabled("pcfactory")


def test_shopify_uses_plain_identified_client():
    from glitchhunter_ai.scrapers.direct_reader import SHOPIFY_USER_AGENT

    reader = DirectStoreReader()
    assert reader.shopify_client is not reader.client                 # no la imitación de navegador
    assert reader.shopify_client.headers["User-Agent"] == SHOPIFY_USER_AGENT
    injected = MagicMock()
    assert DirectStoreReader(client=injected).shopify_client is injected


def test_shopify_sale_collection_ignores_product_lines_named_black():
    cols = [{"handle": "black-hole", "title": "Black Hole", "products_count": 40},
            {"handle": "black-friday", "title": "Black Friday", "products_count": 12}]
    assert pick_shopify_sale_collection(cols) == "black-friday"
    assert pick_shopify_sale_collection(cols[:1]) is None


def test_category_pagination_rotates_pages_and_wraps():
    seen_urls = []
    empty_on_call = set()
    duplicate_three = False
    calls = 0
    reader = DirectStoreReader(client=MagicMock(), backoff=StoreBackoff(clock=_Clock()))

    def _make_item(store_id, category_hint, idx=1):
        return ProductItem(
            id=f"{store_id}-item-{idx}", store_id=store_id, store_name=store_id.title(),
            title=f"Smartphone Pro 5G {idx}", category=category_hint,
            current_price=199990, normal_price=299990,
            url=f"https://example.com/{store_id}/{idx}", source="direct",
        )

    def fake_listing(store_id, url, category_hint, tolerant=False):
        nonlocal calls
        calls += 1
        seen_urls.append(url)
        if calls in empty_on_call:
            return []
        if duplicate_three:
            return [_make_item(store_id, category_hint, idx) for idx in (1, 2, 3)]
        return [_make_item(store_id, category_hint, 1)]

    reader._listing = fake_listing
    base_fal = "https://www.falabella.com/falabella-cl/category/cat70008/Smartphones-y-Telefonia"
    for _ in range(CATEGORY_MAX_PAGES + 1):
        reader.fetch_category("falabella", "smartphones")
    assert seen_urls == [base_fal] + [f"{base_fal}?page={p}" for p in range(2, CATEGORY_MAX_PAGES + 1)] + [base_fal]

    # Si una página intermedia devuelve [], vuelve de inmediato a la página 1 en la siguiente llamada.
    seen_urls.clear()
    empty_on_call.add(calls + 2)
    reader.fetch_category("ripley", "smartphones")
    reader.fetch_category("ripley", "smartphones")
    reader.fetch_category("ripley", "smartphones")
    assert seen_urls == [
        "https://simple.ripley.cl/tecno/celulares",
        "https://simple.ripley.cl/tecno/celulares?page=2",
        "https://simple.ripley.cl/tecno/celulares",
    ]

    # Si page=2 repite los mismos >=3 IDs que page=1, corta el recorrido y vuelve a página 1.
    seen_urls.clear()
    duplicate_three = True
    reader.fetch_category("falabella", "notebooks")
    reader.fetch_category("falabella", "notebooks")
    reader.fetch_category("falabella", "notebooks")
    assert seen_urls == [
        "https://www.falabella.com/falabella-cl/category/cat2018/Computacion",
        "https://www.falabella.com/falabella-cl/category/cat2018/Computacion?page=2",
        "https://www.falabella.com/falabella-cl/category/cat2018/Computacion",
    ]

    # Paris alterna entre orden por defecto y mejor descuento (?sortby=best-discount+desc).
    seen_urls.clear()
    duplicate_three = False
    for _ in range(3):
        reader.fetch_category("paris", "smartphones")
    assert seen_urls == [
        "https://www.paris.cl/tecnologia/celulares/",
        "https://www.paris.cl/tecnologia/celulares/?sortby=best-discount+desc",
        "https://www.paris.cl/tecnologia/celulares/",
    ]

    seen_urls.clear()
    reader.fetch_category("abc", "smartphones")
    reader.fetch_category("abc", "smartphones")
    assert seen_urls[0] == "https://www.abc.cl/tecnologia/celulares/?sz=48"
    assert seen_urls[1] == "https://www.abc.cl/tecnologia/celulares/?sz=48&start=48"


def test_vtex_hot_lane_rotates_offsets_when_full_page():
    reader = DirectStoreReader(client=MagicMock(), backoff=StoreBackoff(clock=_Clock()))
    full_page = [
        ProductItem(
            id=f"sony-{i}", store_id="sony", store_name="Sony",
            title=f"Audífono Sony {i}", category="audio",
            current_price=49990, normal_price=99990,
            url=f"https://store.sony.cl/item-{i}/p", source="direct",
        )
        for i in range(50)
    ]
    offsets = []

    def fake_vtex_query(store_id, params):
        offsets.append(params["_from"])
        return list(full_page), 250

    reader._vtex_query = fake_vtex_query
    for _ in range(5):
        items = reader.fetch_hot("sony")
        assert len(items) == 50
    assert offsets == [0, 50, 100, 150, 0]


def test_continuous_reader_batches_large_watchlist(tmp_path):
    from glitchhunter_ai.core.continuous_reader import ContinuousReader, WATCHLIST_BATCH_SIZE

    db = PriceDatabase(db_path=tmp_path / "wl_batch.db")
    db.init_db()
    reader = MagicMock()
    searched = []
    reader.search.side_effect = lambda sid, kw: searched.append((sid, kw)) or []
    reader.fetch_category.return_value = []
    pipe = GlitchHuntingPipeline(db=db, direct_reader=reader)
    kws = [f"kw{i:02d}" for i in range(12)]
    cr = ContinuousReader(
        pipe, workers=2, hot_interval=30.0, deep_interval=60.0, deep_min=20.0, deep_max=120.0,
        categories=[], watchlist_provider=lambda: kws,
    )

    _, calls1 = cr._fetch_with_calls("ripley", "recorrido")
    _, calls2 = cr._fetch_with_calls("ripley", "recorrido")
    cr.pool.shutdown(wait=False)
    assert WATCHLIST_BATCH_SIZE == 8
    assert len(searched) == 16
    assert [kw for _, kw in searched[:8]] == kws[:8]
    assert [kw for _, kw in searched[8:]] == kws[8:] + kws[:4]
    assert calls1 == 8 + len(cr.categories) and calls2 == 8 + len(cr.categories)


def test_publish_new_deals_includes_25pct_tech_with_30k_savings(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "tech25.db")
    db.init_db()
    pipe = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    pipe.telegram_dispatcher.send_alert = MagicMock(return_value=True)

    tech_item = ProductItem(
        id="rip-nb-4060", store_id="ripley", store_name="Ripley",
        title="Notebook Gamer Lenovo RTX 4060", category="notebooks",
        current_price=350000, normal_price=500000,
        url="https://simple.ripley.cl/nb-4060", source="direct",
    )
    cloth_item = ProductItem(
        id="rip-chaqueta", store_id="ripley", store_name="Ripley",
        title="Chaqueta Outdoor Impermeable Negra", category="ropa",
        current_price=84000, normal_price=120000,
        url="https://simple.ripley.cl/chaqueta", source="direct",
    )
    pipe.process_store_products("ripley", [tech_item, cloth_item], send_notifications=False)
    published = pipe.publish_new_deals()
    assert published == 1
    feed_pids = {p["product_id"] for p in db.get_feed(limit=10)}
    assert "rip-nb-4060" in feed_pids
    assert "rip-chaqueta" not in feed_pids


def test_live_search_pending_and_external_ingest(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "live_ext.db")
    db.init_db()
    reader = MagicMock()
    reader.search.return_value = []
    pipe = GlitchHuntingPipeline(db=db, direct_reader=reader)
    pipe.telegram_dispatcher.send_alert = MagicMock(return_value=True)

    ls = LiveSearch(pipe, workers=2)
    snap = ls.start("rtx 4070")
    assert snap is not None
    pending = ls.pending_jobs()
    assert any(j["id"] == snap["id"] and j["query"] == "rtx 4070" for j in pending)

    ext_item = ProductItem(
        id="rip-rtx-4070", store_id="ripley", store_name="Ripley",
        title="Tarjeta de Video MSI RTX 4070", category="componentes",
        current_price=499990, normal_price=699990,
        url="https://simple.ripley.cl/rtx-4070", source="direct",
    )
    added = ls.ingest_external_results(snap["id"], "ripley", [ext_item])
    assert added == 1
    updated = ls.snapshot(snap["id"])
    assert updated["count"] == 1
    assert updated["results"][0]["product_id"] == "rip-rtx-4070"


def test_best_deals_cheap_outlet_bulk_not_boosted():
    from glitchhunter_ai.core.best_deals import score_post

    outlet_post = {
        "kind": "glitch",
        "best_price": 5000,
        "discount_pct": 70.6,
        "payload": {
            "bulk_price_count": 5,
            "product": {"normal_price": 16990},
            "verdict": {"is_missing_zero": False, "absolute_savings_clp": 11990},
        },
    }
    score, reasons = score_post(outlet_post)
    assert "Error de precio verificado" not in reasons
    assert score < 70.0  # no recibe +15 (glitch) ni +4 (bulk_price_count)


def test_big_store_hot_lane_rotates_discount_sections():
    for big_store in ("falabella", "ripley", "paris", "sodimac", "abc"):
        assert big_store in HOT_LANE_STORES

    seen = []
    reader = DirectStoreReader(client=MagicMock(), backoff=StoreBackoff(clock=_Clock()))

    def fake_listing(store_id, url, category_hint, tolerant=False):
        seen.append((store_id, url, category_hint, tolerant))
        return [ProductItem(
            id=f"{store_id}-hot-{len(seen)}", store_id=store_id, store_name=store_id.title(),
            title="Smart TV OLED 55", category=category_hint,
            current_price=399990, normal_price=899990,
            url=f"https://example.com/{store_id}/hot/{len(seen)}", source="direct",
        )]

    reader._listing = fake_listing

    for _ in range(BIG_HOT_MAX_PAGES + 1):
        reader.fetch_hot("falabella")
    fal_urls = [u for s, u, _, _ in seen if s == "falabella"]
    assert "f.range.derived.variant.discount=40%25" in fal_urls[0]
    assert fal_urls[1].endswith("&page=2")
    assert fal_urls[2].endswith(f"&page={BIG_HOT_MAX_PAGES}")
    assert "/Smartphones-y-Telefonia?" in fal_urls[3] and "&page=" not in fal_urls[3]

    seen.clear()
    for _ in range(BIG_HOT_MAX_PAGES + 1):
        reader.fetch_hot("ripley")
    rip_urls = [u for s, u, _, _ in seen if s == "ripley"]
    assert rip_urls[0].endswith("/outlet/tecno")
    assert rip_urls[1].endswith("/outlet/tecno?page=2")
    assert rip_urls[2].endswith(f"/outlet/tecno?page={BIG_HOT_MAX_PAGES}")
    assert rip_urls[3].endswith("/outlet/tecno/telefonia")

    seen.clear()
    reader.fetch_hot("paris")
    reader.fetch_hot("paris")
    paris_urls = [u for s, u, _, _ in seen if s == "paris"]
    assert all("?sortby=best-discount+desc" in u and "page=" not in u for u in paris_urls)
    assert paris_urls[0] != paris_urls[1]
