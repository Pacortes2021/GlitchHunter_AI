"""
Tests del Canal de Ofertas: cada oferta se publica una sola vez (como un canal de Telegram).
Cubre:
1. Una oferta ya publicada no se repite en escaneos posteriores.
2. Solo se vuelve a publicar si el producto baja aún más de precio.
3. /api/feed entrega las publicaciones con hora fija y permite pedir solo las nuevas (after_id).
"""

from fastapi.testclient import TestClient

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline


def _deal(price: float, pid: str = "paris-tv-1") -> ProductItem:
    return ProductItem(
        id=pid,
        store_id="paris",
        store_name="Paris.cl",
        title="Smart TV LG 55 UHD 55UR7800",
        category="televisores",
        current_price=price,
        normal_price=599990.0,
        url=f"https://www.paris.cl/{pid}.html",
        in_stock=True,
    )


def _pipeline(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "feed.db")
    db.init_db()
    return db, GlitchHuntingPipeline(db=db)


def test_published_offer_is_not_repeated_on_later_scans(tmp_path):
    db, pipeline = _pipeline(tmp_path)
    db.upsert_product(_deal(299990.0))  # 50% de descuento

    assert pipeline.publish_new_deals() == 1
    first = db.get_feed()
    assert len(first) == 1

    # Escaneos posteriores ven el mismo producto al mismo precio: no se publica de nuevo
    db.upsert_product(_deal(299990.0))
    assert pipeline.publish_new_deals() == 0
    db.upsert_product(_deal(299990.0))
    assert pipeline.publish_new_deals() == 0

    feed = db.get_feed()
    assert len(feed) == 1
    # La hora de la publicación es la de recepción y no cambia
    assert feed[0]["received_at"] == first[0]["received_at"]


def test_offer_is_republished_only_when_price_drops_further(tmp_path):
    db, pipeline = _pipeline(tmp_path)
    db.upsert_product(_deal(299990.0))
    pipeline.publish_new_deals()

    # Sube de precio (sigue en rango 40-64%): no es una oferta nueva
    db.upsert_product(_deal(319990.0))
    assert pipeline.publish_new_deals() == 0

    # Baja más que en la publicación original: oferta nueva
    db.upsert_product(_deal(259990.0))
    assert pipeline.publish_new_deals() == 1

    feed = db.get_feed()
    assert [p["best_price"] for p in feed] == [259990.0, 299990.0]  # más reciente primero


def test_glitch_alert_is_published_once(tmp_path):
    db, pipeline = _pipeline(tmp_path)
    glitch = _deal(59999.0, pid="paris-tv-glitch")  # ~10x: error de precio verificado (falta un cero)

    assert pipeline.process_product(glitch, send_notifications=False) is not None
    # El mismo glitch en el siguiente escaneo no genera alerta ni publicación nueva
    assert pipeline.process_product(glitch, send_notifications=False) is None

    posts = db.get_feed()
    assert len(posts) == 1
    assert posts[0]["kind"] == "glitch"


def test_feed_api_returns_only_new_posts_after_id(tmp_path, monkeypatch):
    from glitchhunter_ai.web import server

    db, pipeline = _pipeline(tmp_path)
    monkeypatch.setattr(server, "db", db)
    client = TestClient(server.app)

    db.upsert_product(_deal(299990.0, pid="paris-a"))
    pipeline.publish_new_deals()

    first = client.get("/api/feed").json()
    assert len(first) == 1
    post = first[0]
    assert post["feed_kind"] == "deal"
    assert post["product"]["id"] == "paris-a"
    assert post["created_at"] == post["received_at"]
    last_id = post["feed_id"]

    # Sin novedades: la página no recibe nada que volver a dibujar
    assert client.get(f"/api/feed?after_id={last_id}").json() == []

    db.upsert_product(_deal(279990.0, pid="paris-b"))
    pipeline.publish_new_deals()
    new_posts = client.get(f"/api/feed?after_id={last_id}").json()
    assert [p["product"]["id"] for p in new_posts] == ["paris-b"]



def test_unverified_big_discount_is_published_as_deal_without_push(tmp_path):
    """
    Un -70% contra el precio tachado de la tienda, sin historial, sin referencia de mercado y sin
    patrón ~10x, no es evidencia de error de precio (perfumes y cremas lo muestran todo el año):
    se publica como oferta y no se notifica.
    """
    from unittest.mock import MagicMock

    db = PriceDatabase(db_path=tmp_path / "feed.db")
    db.init_db()
    telegram = MagicMock(simulation_mode=False, is_simulation=False)
    pipeline = GlitchHuntingPipeline(db=db, telegram_dispatcher=telegram,
                                     ntfy_dispatcher=MagicMock(), discord_dispatcher=MagicMock())
    perfume = ProductItem(
        id="falabella-perfume-1", store_id="falabella", store_name="Falabella",
        title="Perfume Hombre Red Men Edp 50 Ml", category="belleza",
        current_price=24290.0, normal_price=80990.0,
        url="https://www.falabella.com/falabella-cl/product/1/perfume/1", in_stock=True,
    )
    pipeline.process_product(perfume, send_notifications=True)

    posts = db.get_feed()
    assert [p["kind"] for p in posts] == ["deal"]
    telegram.send_alert.assert_not_called()


def test_refurbished_products_are_ignored(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "feed.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db)
    iphone = ProductItem(
        id="ripley-iphone13-reac", store_id="ripley", store_name="Ripley",
        title="IPHONE 13 128 GB NEGRO - REACONDICIONADO", category="smartphones",
        current_price=329990.0, normal_price=899990.0,
        url="https://simple.ripley.cl/iphone-13-reacondicionado-2000400000001p", in_stock=True,
    )
    assert pipeline.process_product(iphone, send_notifications=False) is None
    assert db.get_feed() == []



def test_feed_uses_one_name_per_store(tmp_path, monkeypatch):
    from glitchhunter_ai.web import server

    db = PriceDatabase(db_path=tmp_path / "feed.db")
    db.init_db()
    monkeypatch.setattr(server, "db", db)
    for pid, name in (("lider-1", "Líder Chile"), ("lider-2", "Líder")):
        db.publish_feed_offer(kind="deal", product_id=pid, store_id="lider", best_price=10000.0, discount_pct=50.0,
                              payload={"product_id": pid, "store_id": "lider", "store_name": name, "title": "x",
                                       "url": f"https://www.lider.cl/{pid}", "current_price": 10000, "normal_price": 20000})
    names = {p["product"]["store_name"] for p in TestClient(server.app).get("/api/feed").json()}
    assert names == {"Líder"}


# =====================================================================
# Vigencia de las publicaciones (ofertas expiradas)
# =====================================================================
def _published(tmp_path, price=299990.0):
    db, pipeline = _pipeline(tmp_path)
    db.upsert_product(_deal(price))
    pipeline.publish_new_deals()
    return db, pipeline, db.get_feed()[0]["id"]


def _status(db, post_id):
    with db.get_connection() as conn:
        return dict(conn.execute("SELECT status, expired_reason, price_now FROM offer_feed WHERE id = ?", (post_id,)).fetchone())


def test_offer_expires_when_price_goes_up_and_reactivates_when_it_returns(tmp_path):
    db, pipeline, pid = _published(tmp_path)
    assert pipeline.refresh_feed_statuses() == 0
    assert _status(db, pid)["status"] == "active"

    db.upsert_product(_deal(449990.0))              # la tienda corrigió el precio
    assert pipeline.refresh_feed_statuses() == 1
    st = _status(db, pid)
    assert (st["status"], st["expired_reason"], st["price_now"]) == ("expired", "price_up", 449990.0)

    db.upsert_product(_deal(299990.0))              # vuelve al precio publicado
    pipeline.refresh_feed_statuses()
    assert _status(db, pid)["status"] == "active"


def test_offer_expires_when_out_of_stock(tmp_path):
    db, pipeline, pid = _published(tmp_path)
    db.upsert_product(_deal(299990.0).model_copy(update={"in_stock": False}))
    pipeline.refresh_feed_statuses()
    assert _status(db, pid)["expired_reason"] == "out_of_stock"


def test_offer_not_seen_for_a_day_is_unconfirmed(tmp_path):
    from datetime import datetime, timedelta, timezone

    db, pipeline, pid = _published(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_connection() as conn:
        conn.execute("UPDATE products SET last_updated = ?", (old,))
        conn.commit()
    pipeline.refresh_feed_statuses()
    assert _status(db, pid)["expired_reason"] == "not_seen"


def test_feed_status_api_returns_only_changes_since(tmp_path, monkeypatch):
    from glitchhunter_ai.web import server

    db, pipeline, pid = _published(tmp_path)
    monkeypatch.setattr(server, "db", db)
    client = TestClient(server.app)

    first = client.get("/api/feed/status").json()
    assert first["changes"] == []
    since = first["now"]

    db.upsert_product(_deal(449990.0))
    pipeline.refresh_feed_statuses()
    changes = client.get("/api/feed/status", params={"since": since}).json()["changes"]
    assert [(c["feed_id"], c["status"], c["expired_reason"]) for c in changes] == [(pid, "expired", "price_up")]

    post = client.get("/api/feed").json()[0]
    assert post["status"] == "expired" and post["price_now"] == 449990.0
