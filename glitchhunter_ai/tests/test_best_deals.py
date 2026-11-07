"""Curador "Lo mejor": puntaje, variedad, registro diario y endpoints."""

from datetime import datetime, timedelta, timezone

from glitchhunter_ai.core.best_deals import BestDealsCurator, _title_family, score_post
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import ProductItem

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "best.db")
    db.init_db()
    return db


def _publish(db, pid, store, title, price, normal, kind="deal", **extra):
    db.upsert_product(ProductItem(id=pid, store_id=store, store_name=store, title=title, category="otros",
                                  current_price=price, normal_price=normal, url=f"https://x.cl/{pid}"))
    if kind == "deal":
        payload = {"product_id": pid, "title": title, "normal_price": normal, "current_price": price,
                   "savings_clp": normal - price}
    else:
        payload = {"product": {"title": title, "normal_price": normal}, "verdict": {
            "absolute_savings_clp": normal - price, "is_missing_zero": extra.get("missing_zero", False)}}
    return db.publish_feed_offer(kind=kind, product_id=pid, store_id=store, best_price=price,
                                 discount_pct=100 * (1 - price / normal), payload=payload)


def test_score_prefers_big_savings_and_evidence():
    small = {"kind": "deal", "best_price": 3000, "discount_pct": 70, "payload": {"normal_price": 10000}}
    big = {"kind": "deal", "best_price": 400000, "discount_pct": 60, "payload": {"normal_price": 1000000}}
    glitch = {"kind": "glitch", "best_price": 49990, "discount_pct": 89,
              "payload": {"product": {"normal_price": 450990}, "verdict": {"is_missing_zero": True}}}
    s_small, _ = score_post(small)
    s_big, reasons = score_post(big)
    s_glitch, g_reasons = score_post(glitch)
    assert s_big > s_small and "Ahorras $600.000" in reasons
    assert s_glitch > s_big and "Parece que le falta un cero" in g_reasons
    s_low, low_reasons = score_post(big, hist={"min": 450000, "avg": 900000, "n": 8})
    assert s_low > s_big and "El precio más bajo que hemos visto" in low_reasons
    assert score_post(big, votes={"down": 3})[0] < s_big


def test_title_family_ignores_sizes():
    assert _title_family("POP & TEE: My Hero Academia - Katsuki(MT)") == _title_family("POP & TEE: My Hero Academia - Katsuki(XL)")


def test_rank_dedupes_caps_per_store_and_skips_withdrawn(tmp_path):
    db = _db(tmp_path)
    for i in range(5):
        _publish(db, f"kliper-{i}", "kliper", f"Chaqueta modelo {i}", 50000, 170000)
    _publish(db, "weplay-1", "weplay", "Polera Katsuki(MT)", 9990, 39990)
    _publish(db, "weplay-2", "weplay", "Polera Katsuki(XL)", 9990, 39990)
    gid = _publish(db, "mk-gessi", "mk", "Mezclador Gessi", 49990, 450990, kind="glitch", missing_zero=True)
    wid = _publish(db, "electrolux-g", "electrolux", "Refrigerador 420L", 100000, 900000, kind="glitch")
    db.withdraw_feed_offers([wid])
    picks = BestDealsCurator(db).rank(days=1, limit=10, per_store=2)
    ids = [p["product_id"] for p in picks]
    assert ids[0] == "mk-gessi"
    assert sum(1 for i in ids if i.startswith("kliper")) == 2
    assert sum(1 for i in ids if i.startswith("weplay")) == 1
    assert "electrolux-g" not in ids and gid


def test_snapshot_keeps_daily_top_even_after_expiring(tmp_path):
    db = _db(tmp_path)
    _publish(db, "mk-gessi", "mk", "Mezclador Gessi", 49990, 450990, kind="glitch", missing_zero=True)
    curator = BestDealsCurator(db)
    assert curator.snapshot_day(datetime.now(timezone.utc)) == 1
    with db.get_connection() as conn:
        conn.execute("UPDATE offer_feed SET status='expired', expired_reason='price_up';")
        conn.commit()
    hist = curator.history(days=3)
    assert hist[0]["posts"][0]["product_id"] == "mk-gessi" and hist[0]["posts"][0]["status"] == "expired"


def test_best_endpoints(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import glitchhunter_ai.web.server as server

    db = _db(tmp_path)
    _publish(db, "mk-gessi", "mk", "Mezclador Gessi", 49990, 450990, kind="glitch", missing_zero=True)
    curator = BestDealsCurator(db)
    monkeypatch.setattr(server, "best_deals", curator)
    client = TestClient(server.app)
    res = client.get("/api/best", params={"days": 2})
    assert res.status_code == 200
    card = res.json()[0]
    assert card["best_rank"] == 1 and card["feed_kind"] == "glitch" and card["best_reasons"]
    curator.snapshot_day(datetime.now(timezone.utc))
    hist = client.get("/api/best/history").json()
    assert hist[0]["posts"][0]["best_rank"] == 1
    assert client.get("/api/best", params={"days": 0}).status_code == 422


def test_backfill_saves_previous_days(tmp_path):
    db = _db(tmp_path)
    _publish(db, "mk-gessi", "mk", "Mezclador Gessi", 49990, 450990, kind="glitch", missing_zero=True)
    later = datetime.now(timezone.utc) + timedelta(days=2)
    curator = BestDealsCurator(db, clock=lambda: later)
    assert curator.backfill(days=3) == 1                 # solo el día en que llegó la publicación
    assert curator.backfill(days=3) == 0                 # ya guardado
    assert curator.history(days=5)[0]["posts"][0]["product_id"] == "mk-gessi"
