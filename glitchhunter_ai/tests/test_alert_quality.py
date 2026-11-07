"""Falsos positivos encontrados en producción (noviembre 2026)."""

from unittest.mock import MagicMock

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.core.price_signals import detect_bulk_prices
from glitchhunter_ai.scrapers.direct_reader import parse_vtex_products


def _vtex(name, link, price, list_price, without_discount, multiplier=1.0):
    return {"productName": name, "link": link, "items": [{
        "unitMultiplier": multiplier, "measurementUnit": "m²" if multiplier != 1 else "un", "images": [],
        "sellers": [{"commertialOffer": {"Price": price, "ListPrice": list_price,
                                          "PriceWithoutDiscount": without_discount, "AvailableQuantity": 5}}],
    }]}


def test_vtex_price_per_square_meter_is_not_a_discount():
    # MK: porcelanato a $13.990,27 el m² (caja de 1,44 m²) con ListPrice $59.790 en otra unidad
    raw = [
        _vtex("Porcelanato Mars Negro Mate 30x60 cm", "https://www.mk.cl/kpp110862-porcelanatos-klipen-mars/p",
              13990.27, 59790.0, 13990.0, multiplier=1.44),
        _vtex("Taladro Percutor 650W", "https://www.mk.cl/taladro-650w/p", 29990.0, 49990.0, 29990.0),
    ]
    tile, drill = parse_vtex_products("mk", raw)
    assert tile.normal_price == tile.current_price          # sin descuento inventado
    assert drill.normal_price == 49990.0                    # productos por unidad: sin cambios


def _p(pid, price, normal, store="privilege"):
    return ProductItem(id=pid, store_id=store, store_name=store, title=f"Prenda {pid}", category="moda",
                       current_price=price, normal_price=normal, url=f"https://x.cl/{pid}")


def test_outlet_price_point_is_not_bulk_load_error():
    # Liquidación "todo a $14.990" (60-72%): campaña normal, no error de carga
    outlet = [_p("a", 14990, 46990), _p("b", 14990, 49990), _p("c", 14990, 39990), _p("d", 14990, 52990)]
    assert detect_bulk_prices(outlet) == {}
    # Mismo patrón pero un producto queda a -85%: sí es sospechoso
    assert len(detect_bulk_prices(outlet + [_p("e", 14990, 99990)])) == 5
    # Precio que no es de campaña ($14.137) en muchos productos distintos: sospechoso
    odd = [_p(f"o{i}", 14137, n) for i, n in enumerate((39990, 46990, 49990, 52990))]
    assert len(detect_bulk_prices(odd)) == 4


def test_service_items_are_never_published(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "q.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    pipeline.telegram_dispatcher.send_alert = MagicMock(return_value=True)
    warranty = ProductItem(id="electrolux-gext1", store_id="electrolux", store_name="Electrolux",
                           title="Garantía Extendida GEXT1 Inverter 18000 BTUs", category="otros",
                           current_price=10184, normal_price=46990, url="https://x.cl/gext1")
    assert pipeline.process_product(warranty, send_notifications=False) is None


def _feed_db(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "feed.db")
    db.init_db()
    return db


def test_deal_expires_when_store_drops_the_discount(tmp_path):
    db = _feed_db(tmp_path)
    tile = ProductItem(id="mk-tile", store_id="mk", store_name="MK", title="Porcelanato Mars", category="otros",
                       current_price=13990, normal_price=59790, url="https://x.cl/tile")
    db.upsert_product(tile)
    fid = db.publish_feed_offer(kind="deal", product_id=tile.id, store_id="mk", best_price=13990,
                                discount_pct=76.6, payload={"product_id": tile.id, "title": tile.title})
    assert fid
    # La lectura corregida ya no muestra precio tachado
    db.upsert_product(tile.model_copy(update={"normal_price": 13990}))
    db.refresh_feed_statuses()
    post = db.get_feed()[0]
    assert (post["status"], post["expired_reason"]) == ("expired", "no_discount")


def test_withdrawn_posts_are_hidden_and_never_reactivated(tmp_path):
    db = _feed_db(tmp_path)
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    warranty = ProductItem(id="electrolux-g", store_id="electrolux", store_name="Electrolux",
                           title="Garantía Extendida GEXT2 Inverter 12.000 BTUs", category="otros",
                           current_price=21048, normal_price=21048, url="https://x.cl/g")
    shirt = ProductItem(id="ae-1", store_id="americaneagle", store_name="AE", title="Camisa AE", category="moda",
                        current_price=12990, normal_price=44990, url="https://x.cl/ae")
    real = ProductItem(id="mk-gessi", store_id="mk", store_name="MK", title="Mezclador Gessi", category="otros",
                       current_price=49990, normal_price=450990, url="https://x.cl/gessi")
    for p in (warranty, shirt, real):
        db.upsert_product(p)
    db.publish_feed_offer(kind="glitch", product_id=warranty.id, store_id="electrolux", best_price=21048, discount_pct=67,
                          payload={"product": {"title": warranty.title}, "verdict": {"reason": "Caída vs su precio habitual"}})
    db.publish_feed_offer(kind="glitch", product_id=shirt.id, store_id="americaneagle", best_price=12990, discount_pct=71,
                          payload={"product": {"title": shirt.title}, "verdict": {"reason": "Descuento masivo"},
                                   "bulk_price_count": 6})
    db.publish_feed_offer(kind="glitch", product_id=real.id, store_id="mk", best_price=49990, discount_pct=89,
                          payload={"product": {"title": real.title}, "verdict": {"is_missing_zero": True}})
    assert pipeline.withdraw_false_offers() == {"servicios": 1, "liquidaciones": 1}
    assert [p["product_id"] for p in db.get_feed()] == ["mk-gessi"]
    db.refresh_feed_statuses()
    assert [p["product_id"] for p in db.get_feed()] == ["mk-gessi"]     # siguen retiradas
