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
