"""Apple y la watchlist se publican desde un descuento menor (rara vez bajan 40%)."""

from unittest.mock import MagicMock

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline


def _p(pid, title, price, normal, store="lider"):
    return ProductItem(id=pid, store_id=store, store_name=store, title=title, category="otros",
                       current_price=price, normal_price=normal, url=f"https://x.cl/{pid}")


def test_apple_and_watchlist_publish_from_lower_discount(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "prio.db")
    db.init_db()
    db.add_watchlist_keyword("rtx 4070")
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    products = [
        _p("lider-iphone15", "Apple iPhone 15 128GB Negro", 799990, 999990),          # -20%: sí
        _p("lider-airpods", "AirPods Pro 2da generación", 189990, 249990),            # -24%: sí
        _p("pcf-rtx", "Tarjeta de Video MSI RTX 4070 Ventus 12GB", 559990, 699990, store="pcfactory"),  # watchlist -20%
        _p("lider-polera", "Polera Algodón Hombre", 7990, 9990),                       # -20% sin prioridad: no
        _p("lider-funda", "Funda para iPhone 15 Transparente", 5990, 9990),            # accesorio: no
        _p("ripley-reac", "IPHONE 13 128 GB NEGRO - REACONDICIONADO", 499990, 649990, store="ripley"),  # no
        _p("lider-iphone-10", "Apple iPhone 14 128GB", 899990, 949990),               # -5%: no
    ]
    for p in products:
        db.upsert_product(p)
    assert pipeline.publish_new_deals() == 3
    published = {p["product_id"] for p in db.get_feed()}
    assert published == {"lider-iphone15", "lider-airpods", "pcf-rtx"}
    assert pipeline.publish_new_deals() == 0          # no se repiten


def test_title_terms_filter_in_sql(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "terms.db")
    db.init_db()
    db.upsert_product(_p("a", "Apple MacBook Air M3", 999990, 1299990))
    db.upsert_product(_p("b", "Notebook Lenovo IdeaPad", 399990, 599990))
    rows = db.get_commercial_deals(min_discount=15, max_discount=101, title_terms=["macbook"])
    assert [r["product_id"] for r in rows] == ["a"]


def test_priority_deals_are_not_starved_by_big_backlog(tmp_path, monkeypatch):
    import glitchhunter_ai.config as config
    monkeypatch.setattr(config, "DEAL_PUBLISH_BATCH", 5)
    db = PriceDatabase(db_path=tmp_path / "backlog.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    for i in range(20):   # muchas ofertas de 50% que llenan el cupo
        db.upsert_product(_p(f"lider-tv-{i}", f"Smart TV Samsung 55 modelo {i}", 300000, 600000))
    db.upsert_product(_p("abc-iphone16e", "Apple iPhone 16e 128GB Blanco", 499990, 699990, store="abc"))
    db.upsert_product(_p("spd-caja", "CAJA ABIERTA/DAÑADA Carcasa Snap iPhone 14 Plus", 7990, 9990, store="spdigital"))
    pipeline.publish_new_deals()
    ids = {p["product_id"] for p in db.get_feed()}
    assert "abc-iphone16e" in ids and "spd-caja" not in ids
