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
    glitch = _deal(49990.0, pid="paris-tv-glitch")  # ~92% de descuento

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
