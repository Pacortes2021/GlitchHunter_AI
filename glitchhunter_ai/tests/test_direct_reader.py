"""
Tests del Modo Híbrido: lectura directa de Falabella y Ripley.
Los fixtures en tests/fixtures/ son extractos reales (recortados) del __NEXT_DATA__ de cada tienda.
Cubre:
1. Parseo de listados y páginas de producto de Falabella y Ripley.
2. IDs canónicos: el mismo producto tiene el mismo ID vía agregador y vía lectura directa.
3. Freno automático ante bloqueos (403/429, verificación anti-bot, página sin datos).
4. Confirmación en tiempo real de candidatos del agregador en el pipeline.
5. Escaneo directo prioritario (watchlist).
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.scrapers.direct_reader import (
    VTEX_STORES,
    DirectStoreReader,
    StoreBackoff,
    parse_falabella_listing,
    parse_falabella_product,
    parse_ripley_listing,
    parse_ripley_product,
)
from glitchhunter_ai.scrapers.product_ids import canonical_product_id

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# =====================================================================
# 1. Parseo de páginas reales
# =====================================================================
def test_parse_falabella_listing_real_structure():
    items = parse_falabella_listing(_fixture("falabella_listing.html"), "smartphones")
    assert len(items) == 3
    s25 = items[0]
    assert s25.id == "falabella-prod129939632"
    assert s25.current_price == 999990      # internetPrice
    assert s25.normal_price == 1469990      # normalPrice
    assert s25.card_price == 949990         # cmrPrice
    assert s25.source == "direct"
    # eventPrice también cuenta como precio de oferta
    assert items[2].current_price == 589990


def test_parse_falabella_product_page():
    url = "https://www.falabella.com/falabella-cl/product/17558931/Celular-Samsung-Galaxy-S25-FE-256GB/17558931"
    item = parse_falabella_product(_fixture("falabella_product.html"), url)
    assert item.id == "falabella-17558931"
    assert (item.current_price, item.normal_price, item.card_price) == (499990, 799990, 479990)
    assert item.in_stock is True


def test_parse_ripley_listing_real_structure():
    items = parse_ripley_listing(_fixture("ripley_listing.html"), "notebooks")
    assert [i.id for i in items] == ["ripley-mpm10003498644", "ripley-2000406555881", "ripley-2000412095517"]
    acer = items[1]
    assert (acer.current_price, acer.normal_price, acer.card_price) == (549990, 729990, None)
    assert acer.url.startswith("https://simple.ripley.cl/notebook-acer-aspire-lite")
    assert acer.url.endswith("2000406555881p")
    # Precio Ripley (tarjeta) menor al de oferta
    assert items[0].card_price == 399990


def test_parse_ripley_product_page():
    url = "https://simple.ripley.cl/notebook-acer-aspire-lite-laptop-al15-41p-r0zy-2-amd-ryzen-7-8-gb-ram-512-gb-ssd-156-fhd-2000406555881p"
    item = parse_ripley_product(_fixture("ripley_product.html"), url)
    assert item.id == "ripley-2000406555881"
    assert (item.current_price, item.normal_price, item.card_price) == (549990, 729990, None)
    assert item.in_stock is True


# =====================================================================
# 2. IDs canónicos compartidos entre agregador y tienda
# =====================================================================
def test_canonical_ids_match_aggregator_urls():
    # URLs con el formato que entrega SoloTodo para las mismas tiendas
    assert canonical_product_id(
        "falabella", "https://www.falabella.com/falabella-cl/product/17558931/Celular-Samsung/17558931"
    ) == "falabella-17558931"
    assert canonical_product_id(
        "ripley", "https://simple.ripley.cl/impresora-laser-brother-hl1202-2000366254404p"
    ) == "ripley-2000366254404"
    assert canonical_product_id(
        "ripley", "https://simple.ripley.cl/notebook-asus-vivobook-go-15-mpm10003382085"
    ) == "ripley-mpm10003382085"
    assert canonical_product_id("lider", "https://www.lider.cl/x-123") is None
    assert canonical_product_id("falabella", None) is None


# =====================================================================
# 3. Freno automático ante bloqueos
# =====================================================================
class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _resp(status=200, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


def test_backoff_pauses_store_after_block_and_resumes():
    clock = _FakeClock()
    backoff = StoreBackoff(base_seconds=600, max_seconds=7200, clock=clock)
    client = MagicMock()
    reader = DirectStoreReader(client=client, backoff=backoff)

    client.get.return_value = _resp(429)
    assert reader.search("falabella", "rtx 4070") == []
    assert backoff.is_paused("falabella")
    # Otras tiendas no se ven afectadas
    assert not backoff.is_paused("ripley")

    # Mientras está en pausa no se vuelve a consultar la tienda
    client.get.reset_mock()
    assert reader.search("falabella", "rtx 4070") == []
    client.get.assert_not_called()

    # Pasada la pausa, una respuesta válida la reactiva y reinicia la espera
    clock.t += 601
    client.get.return_value = _resp(200, _fixture("falabella_listing.html"))
    assert len(reader.search("falabella", "galaxy")) == 3
    assert not backoff.is_paused("falabella")


def test_backoff_is_exponential_and_capped():
    clock = _FakeClock()
    backoff = StoreBackoff(base_seconds=600, max_seconds=2000, clock=clock)
    assert backoff.record_block("ripley", "x") == 600
    assert backoff.record_block("ripley", "x") == 1200
    assert backoff.record_block("ripley", "x") == 2000


def test_bot_challenge_page_triggers_pause():
    backoff = StoreBackoff(clock=_FakeClock())
    client = MagicMock()
    client.get.return_value = _resp(200, "<html><body>Please verify you are human - px-captcha</body></html>")
    reader = DirectStoreReader(client=client, backoff=backoff)
    assert reader.fetch_product("ripley", "https://simple.ripley.cl/x-2000406555881p") is None
    assert backoff.is_paused("ripley")


# =====================================================================
# 4. Confirmación en tiempo real en el pipeline
# =====================================================================
def _aggregator_glitch(**overrides):
    data = dict(
        id="falabella-17558931",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Celular Samsung Galaxy S25 FE 256GB",
        category="smartphones",
        current_price=49990.0,
        normal_price=799990.0,
        url="https://www.falabella.com/falabella-cl/product/17558931/Celular/17558931",
        in_stock=True,
        source="solotodo",
    )
    data.update(overrides)
    return ProductItem(**data)


def _pipeline(tmp_path, reader):
    db = PriceDatabase(db_path=tmp_path / "direct.db")
    db.init_db()
    return GlitchHuntingPipeline(db=db, direct_reader=reader, confirm_with_store=True)


def test_confirmation_discards_glitch_already_corrected_in_store(tmp_path):
    reader = MagicMock()
    # La tienda ya muestra el precio real: el agregador tenía un dato antiguo
    reader.fetch_product.return_value = _aggregator_glitch(current_price=499990.0, card_price=479990.0, source="direct")
    pipeline = _pipeline(tmp_path, reader)

    assert pipeline.process_product(_aggregator_glitch(), send_notifications=False) is None
    reader.fetch_product.assert_called_once()


def test_confirmation_keeps_glitch_with_live_store_prices(tmp_path):
    reader = MagicMock()
    reader.fetch_product.return_value = _aggregator_glitch(current_price=59990.0, source="direct")
    pipeline = _pipeline(tmp_path, reader)

    alert = pipeline.process_product(_aggregator_glitch(), send_notifications=False)
    assert alert is not None
    assert alert.product.current_price == 59990.0
    assert alert.product.source == "direct"


def test_confirmation_falls_back_to_aggregator_when_store_unavailable(tmp_path):
    reader = MagicMock()
    reader.fetch_product.return_value = None  # tienda en pausa o sin respuesta
    pipeline = _pipeline(tmp_path, reader)

    alert = pipeline.process_product(_aggregator_glitch(), send_notifications=False)
    assert alert is not None
    assert alert.product.current_price == 49990.0


def test_confirmation_skips_out_of_stock_and_non_aggregator_items(tmp_path):
    reader = MagicMock()
    reader.fetch_product.return_value = _aggregator_glitch(in_stock=False, source="direct")
    pipeline = _pipeline(tmp_path, reader)
    assert pipeline.process_product(_aggregator_glitch(), send_notifications=False) is None

    # Un producto leído directamente (o de otra tienda) no se vuelve a confirmar
    reader.fetch_product.reset_mock()
    pipeline.process_product(_aggregator_glitch(id="falabella-999", source="direct"), send_notifications=False)
    pipeline.process_product(
        _aggregator_glitch(id="lider-1", store_id="lider", store_name="Líder", url="https://www.lider.cl/ip/producto-1"),
        send_notifications=False,
    )
    reader.fetch_product.assert_not_called()


# =====================================================================
# 5. Escaneo directo prioritario
# =====================================================================
def test_run_direct_scan_searches_watchlist_in_all_direct_stores(tmp_path):
    reader = MagicMock()
    glitch = _aggregator_glitch(source="direct")
    reader.search.side_effect = lambda store, kw: [glitch] if store == "falabella" else []
    reader.fetch_category.return_value = []
    pipeline = _pipeline(tmp_path, reader)

    alerts = pipeline.run_direct_scan(keywords=["galaxy s25"], send_notifications=False)
    assert len(alerts) == 1
    searched = sorted(c.args[0] for c in reader.search.call_args_list)
    assert searched == sorted(["abc", "falabella", "hp", "jumbo", "paris", "pcfactory", "ripley", "sodimac", "winpy", *VTEX_STORES])

    # Sin watchlist ni categorías no consulta nada
    reader.search.reset_mock()
    assert pipeline.run_direct_scan(keywords=[], categories=[], send_notifications=False) == []
    reader.search.assert_not_called()


def test_run_direct_scan_skips_while_full_scan_running(tmp_path):
    reader = MagicMock()
    pipeline = _pipeline(tmp_path, reader)
    pipeline._scan_lock.acquire()
    try:
        assert pipeline.run_direct_scan(keywords=["x"], send_notifications=False) == []
        reader.search.assert_not_called()
    finally:
        pipeline._scan_lock.release()


def test_direct_categories_available_in_both_stores():
    """Categorías verificadas en vivo (02-11-2026) para el escaneo directo de Falabella y Ripley."""
    from glitchhunter_ai.scrapers.direct_reader import CATEGORY_PATHS

    for cat in ("smartphones", "notebooks", "televisores", "consolas"):
        assert cat in CATEGORY_PATHS["falabella"]
        assert cat in CATEGORY_PATHS["ripley"]


# =====================================================================
# 6. Sodimac (lectura directa)
# =====================================================================
def test_parse_sodimac_lista_page():
    """Páginas /lista/ de Sodimac: formato del catálogo del grupo Falabella, con URL /articulo/."""
    from glitchhunter_ai.scrapers.direct_reader import parse_sodimac_listing

    items = parse_sodimac_listing(_fixture("sodimac_lista.html"), "televisores")
    assert len(items) == 3
    tv = items[0]
    assert tv.id == "sodimac-158536333"
    assert tv.store_id == "sodimac"
    assert (tv.current_price, tv.normal_price) == (259990, 339990)
    assert "/sodimac-cl/articulo/158536333/" in tv.url
    assert items[1].card_price == 119990   # cmrPrice


def test_parse_sodimac_search_page():
    """Búsqueda de Sodimac: precios INTERNET/NORMAL; se enlaza como búsqueda por SKU (su ficha /product redirige)."""
    from glitchhunter_ai.scrapers.direct_reader import parse_sodimac_listing

    items = parse_sodimac_listing(_fixture("sodimac_search.html"), "linea_blanca")
    assert [i.id for i in items] == ["sodimac-741630x", "sodimac-6161146", "sodimac-3229688"]
    kids = items[0]
    assert (kids.current_price, kids.normal_price) == (69990, 119990)
    assert kids.url == "https://www.sodimac.cl/sodimac-cl/search?Ntt=741630X"
    assert items[2].current_price == items[2].normal_price == 103990   # solo precio NORMAL


def test_parse_sodimac_product_page():
    from glitchhunter_ai.scrapers.direct_reader import parse_sodimac_product

    url = "https://www.sodimac.cl/sodimac-cl/articulo/158536333/smart-tv-aoc-50-qled-4k-uhd-roku-tv-50q7067"
    item = parse_sodimac_product(_fixture("sodimac_product.html"), url)
    assert item.id == "sodimac-158536333" and item.store_id == "sodimac"
    assert (item.current_price, item.normal_price) == (259990, 339990)


def test_direct_scan_includes_sodimac(tmp_path):
    reader = MagicMock()
    reader.search.return_value = []
    reader.fetch_category.return_value = []
    pipeline = _pipeline(tmp_path, reader)
    pipeline.run_direct_scan(keywords=["refrigerador"], send_notifications=False)
    assert sorted(c.args[0] for c in reader.search.call_args_list) == sorted(["abc", "falabella", "hp", "jumbo", "paris", "pcfactory", "ripley", "sodimac", "winpy", *VTEX_STORES])


# =====================================================================
# 7. PC Factory (API JSON directa)
# =====================================================================
def _pcf_response(page_total=63):
    import json as _json
    data = _json.loads(_fixture("pcfactory_search.json"))
    data["content"]["pageable"]["totalPages"] = page_total
    r = MagicMock(status_code=200)
    r.json.return_value = data
    return r


def test_pcfactory_api_items_parsed_with_slug_url_and_no_outlet():
    client = MagicMock()
    client.get.return_value = _pcf_response()
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_FakeClock()))
    items = reader.search("pcfactory", "moto g15")
    assert len(items) == 3                                    # el producto outlet se descarta
    first = items[0]
    assert first.id == "pcfactory-57363"
    assert first.url == "https://www.pcfactory.cl/producto/57363-motorola-celular-moto-g15-128gb-4gb-gris-liberado"
    assert first.source == "direct" and first.current_price > 0
    params = client.get.call_args.kwargs["params"]
    assert params["search"] == "moto g15"


def test_pcfactory_catalog_slice_rotates_through_all_pages():
    client = MagicMock()
    client.get.return_value = _pcf_response(page_total=5)
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_FakeClock()))

    reader.fetch_catalog_slice("pcfactory", pages=3)
    assert [c.kwargs["params"]["page"] for c in client.get.call_args_list] == [0, 1, 2]
    client.get.reset_mock()
    reader.fetch_catalog_slice("pcfactory", pages=3)
    # Páginas 3 y 4, y vuelve al inicio en el siguiente ciclo
    assert [c.kwargs["params"]["page"] for c in client.get.call_args_list] == [3, 4]
    client.get.reset_mock()
    reader.fetch_catalog_slice("pcfactory", pages=1)
    assert client.get.call_args.kwargs["params"]["page"] == 0
    assert reader.fetch_catalog_slice("falabella") == []


def test_pcfactory_fetch_product_and_backoff():
    client = MagicMock()
    client.get.return_value = _pcf_response()
    clock = _FakeClock()
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=clock))
    item = reader.fetch_product("pcfactory", "https://www.pcfactory.cl/producto/57363-motorola-celular-moto-g15")
    assert item is not None and item.id == "pcfactory-57363"

    client.get.return_value = MagicMock(status_code=429)
    assert reader.search("pcfactory", "rtx") == []
    assert reader.backoff.is_paused("pcfactory")


# =====================================================================
# 8. Paris (Next.js App Router: flujo RSC self.__next_f)
# =====================================================================
def test_parse_paris_listing_commercetools_prices():
    from glitchhunter_ai.scrapers.direct_reader import _paris_products, parse_paris_listing

    items = parse_paris_listing(_fixture("paris_listing.html"), "notebooks")
    assert len(items) == 3
    raw = _paris_products(_fixture("paris_listing.html"))
    for item, src in zip(items, raw):
        prices = src["masterVariant"]["prices"]
        assert item.id == f"paris-{src['key'].lower()}"
        assert item.url == f"https://www.paris.cl/{src['slug']}.html"
        assert item.normal_price == prices["regular"]["value"]["centAmount"]
        assert item.current_price == (prices.get("offer") or prices["regular"])["value"]["centAmount"]
        if "paymentMethod" in prices:
            assert item.card_price == prices["paymentMethod"]["value"]["centAmount"]
        assert item.store_id == "paris" and item.source == "direct"


def test_paris_canonical_id_matches_aggregator_urls():
    url = "https://www.paris.cl/notebook-asus-tuf-gaming-a15-amd-ryzen-7-8gb-ram-512gb-ssd-rtx-3050-156-MKDFYM0YKP.html"
    assert canonical_product_id("paris", url) == "paris-mkdfym0ykp"
    # Formato antiguo (numérico) que aún entregan algunos agregadores
    assert canonical_product_id("paris", "https://www.paris.cl/poleron-paneles-252780.html") == "paris-252780"


def test_paris_page_without_rsc_data_triggers_pause():
    backoff = StoreBackoff(clock=_FakeClock())
    client = MagicMock()
    client.get.return_value = _resp(200, "<html><body>Mantenimiento</body></html>")
    reader = DirectStoreReader(client=client, backoff=backoff)
    assert reader.fetch_category("paris", "notebooks") == []
    assert backoff.is_paused("paris")


# =====================================================================
# 9. ABC (Salesforce Commerce Cloud)
# =====================================================================
def test_parse_abc_listing_three_prices():
    from glitchhunter_ai.scrapers.direct_reader import parse_abc_listing

    items = parse_abc_listing(_fixture("abc_listing.html"), "televisores")
    assert len(items) >= 2
    tv = next(i for i in items if i.id == "abc-29592535")
    assert tv.title == "Smart TV 43 LG Full HD WebOS 43LB6500PSB"
    assert tv.url == "https://www.abc.cl/smart-tv-43-lg-full-hd-webos-43lb6500psb/29592535.html"
    # El "normal" de ABC ($399.990) está inflado: la referencia es el precio internet
    assert (tv.current_price, tv.normal_price, tv.card_price) == (189990, 189990, 169990)
    assert tv.store_id == "abc" and tv.source == "direct" and tv.in_stock


def test_parse_abc_product_page():
    from glitchhunter_ai.scrapers.direct_reader import parse_abc_product

    url = "https://www.abc.cl/smart-tv-43-lg-full-hd-webos-43lb6500psb/29592535.html"
    item = parse_abc_product(_fixture("abc_product.html"), url)
    assert item is not None and item.id == "abc-29592535"
    assert item.title.startswith("Smart TV 43")
    assert (item.current_price, item.card_price) == (189990, 169990)


def test_abc_canonical_id():
    assert canonical_product_id("abc", "https://www.abc.cl/smart-tv-43-lg/29592535.html") == "abc-29592535"


def test_parse_jumbo_listing_prices_and_url():
    from glitchhunter_ai.scrapers.direct_reader import parse_jumbo_listing

    items = {i.id: i for i in parse_jumbo_listing(_fixture("jumbo_listing.html"), "linea_blanca")}
    assert set(items) == {"jumbo-licuadora-3-en-1-n-2086981", "jumbo-mini-pimer-ursus-trotter-ut-klot400p"}
    ninja = items["jumbo-licuadora-3-en-1-n-2086981"]
    assert (ninja.current_price, ninja.normal_price, ninja.card_price) == (219990, 279990, None)
    assert ninja.url == "https://www.jumbo.cl/licuadora-3-en-1-n-2086981/p"
    assert ninja.title == "Licuadora Ninja 3 en 1" and ninja.in_stock and ninja.source == "direct"


def test_parse_jumbo_product_picks_url_product():
    from glitchhunter_ai.scrapers.direct_reader import parse_jumbo_product

    item = parse_jumbo_product(_fixture("jumbo_listing.html"), "https://www.jumbo.cl/mini-pimer-ursus-trotter-ut-klot400p/p")
    assert item is not None and item.id == "jumbo-mini-pimer-ursus-trotter-ut-klot400p"
    assert item.current_price == item.normal_price == 34990


def test_jumbo_canonical_id():
    assert canonical_product_id("jumbo", "https://www.jumbo.cl/licuadora-3-en-1-n-2086981/p") == "jumbo-licuadora-3-en-1-n-2086981"
    assert canonical_product_id("jumbo", "https://www.jumbo.cl/hogar-electro-y-libreria/electro-y-tecnologia") is None


def test_parse_hp_listing_magento_prices():
    from glitchhunter_ai.scrapers.direct_reader import parse_hp_listing

    items = parse_hp_listing(_fixture("hp_listing.html"), "notebooks")
    assert [i.id for i in items] == ["hp-d0dd8la", "hp-bm3e7la"]
    first = items[0]
    assert (first.current_price, first.normal_price) == (559990, 559990)
    assert first.url == "https://www.hp.com/cl-es/shop/hp-14-laptop-14-ep1330la-d0dd8la.html"
    assert first.title.startswith("Notebook HP 14-ep1330la") and first.store_id == "hp" and first.in_stock


def test_parse_hp_product_page():
    from glitchhunter_ai.scrapers.direct_reader import parse_hp_product

    url = "https://www.hp.com/cl-es/shop/hp-laptop-15-fd0252la-b9tw9la.html"
    item = parse_hp_product(_fixture("hp_product.html"), url)
    assert item is not None and item.id == "hp-b9tw9la"
    assert item.current_price == 825220 and item.title.startswith("Notebook HP 15-fd0252la")


def test_parse_winpy_listing_and_product():
    from glitchhunter_ai.scrapers.direct_reader import parse_winpy_listing, parse_winpy_product

    items = {i.id: i for i in parse_winpy_listing(_fixture("winpy_listing.html"), "notebooks")}
    pid = "winpy-notebook-hp-245-g10-de-14-ryzen-3-7330u-8gb-ddr4-256gb-ssd-win11"
    assert pid in items and len(items) == 2
    hp245 = items[pid]
    assert (hp245.current_price, hp245.normal_price) == (367840, 367840)
    assert hp245.url == "https://www.winpy.cl/venta/notebook-hp-245-g10-de-14-ryzen-3-7330u-8gb-ddr4-256gb-ssd-win11/"
    # La ficha entrega el mismo precio (transferencia) que el listado
    item = parse_winpy_product(_fixture("winpy_product.html"), hp245.url)
    assert item is not None and item.id == pid and item.current_price == 367840 and item.in_stock


def test_hp_and_winpy_canonical_ids():
    assert canonical_product_id("hp", "https://www.hp.com/cl-es/shop/hp-laptop-15-fd0252la-b9tw9la.html") == "hp-b9tw9la"
    assert canonical_product_id("hp", "https://www.hp.com/cl-es/shop/notebooks.html") is None
    assert canonical_product_id("winpy", "https://www.winpy.cl/venta/notebook-x/") == "winpy-notebook-x"
    assert canonical_product_id("winpy", "https://www.winpy.cl/portatiles/notebooks/") is None


def _vtex_response(products, total, status=206):
    import json as _json

    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = products
    resp.headers = {"resources": f"0-49/{total}"}
    return resp


def test_parse_vtex_products_sony():
    import json as _json
    from glitchhunter_ai.scrapers.direct_reader import parse_vtex_products

    items = parse_vtex_products("sony", _json.loads(_fixture("vtex_sony.json")))
    assert [i.id for i in items] == ["sony-wf-c510", "sony-wh-ch720n", "sony-wh-ch520"]
    first = items[0]
    assert (first.current_price, first.normal_price) == (29990, 49990)
    assert first.store_name == "Sony Store" and first.url == "https://store.sony.cl/wf-c510/p" and first.in_stock


def test_vtex_catalog_slice_rotates_and_wraps():
    import json as _json

    products = _json.loads(_fixture("vtex_sony.json"))
    client = MagicMock()
    client.get.return_value = _vtex_response(products, total=120)
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_FakeClock()))

    assert len(reader.fetch_catalog_slice("sony", pages=10)) == 9   # 3 páginas de 50 cubren 120
    offsets = [c.kwargs["params"]["_from"] for c in client.get.call_args_list]
    assert offsets == [0, 50, 100]
    client.get.reset_mock()
    reader.fetch_catalog_slice("sony", pages=1)
    assert client.get.call_args.kwargs["params"]["_from"] == 0   # vuelve al inicio
    assert client.get.call_args.args[0] == "https://store.sony.cl/api/catalog_system/pub/products/search"


def test_vtex_fetch_product_and_search():
    import json as _json

    products = _json.loads(_fixture("vtex_sony.json"))
    client = MagicMock()
    client.get.return_value = _vtex_response(products[:1], total=1, status=200)
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_FakeClock()))

    item = reader.fetch_product("sony", "https://store.sony.cl/wf-c510/p")
    assert item is not None and item.id == "sony-wf-c510" and item.current_price == 29990
    assert client.get.call_args.args[0].endswith("/products/search/wf-c510/p")
    reader.search("sony", "audifonos")
    assert client.get.call_args.kwargs["params"]["ft"] == "audifonos"


def test_vtex_block_pauses_store():
    client = MagicMock()
    client.get.return_value = _vtex_response([], total=0, status=429)
    reader = DirectStoreReader(client=client, backoff=StoreBackoff(clock=_FakeClock()))
    assert reader.fetch_catalog_slice("fensa", pages=2) == []
    assert reader.backoff.is_paused("fensa") and not reader.backoff.is_paused("sony")
