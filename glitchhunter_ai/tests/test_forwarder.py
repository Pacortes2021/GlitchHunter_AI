"""Unir paneles: el PC envía sus lecturas al servidor (core/forwarder.py y POST /api/ingest)."""

import json
from unittest.mock import MagicMock

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.forwarder import Forwarder
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline


def _product(pid, store="ripley", price=50000, normal=100000):
    return ProductItem(id=pid, store_id=store, store_name="Ripley", title="Notebook Lenovo IdeaPad 5",
                       category="notebooks", current_price=price, normal_price=normal,
                       url=f"https://simple.ripley.cl/{pid}", source="direct")


class _FakePost:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, json.loads(body), headers))
        code = self.codes.pop(0)
        if isinstance(code, Exception):
            raise code
        return code


def test_forwarder_sends_retries_and_drops_rejected():
    post = _FakePost([200, OSError("túnel caído"), 503, 200, 401])
    fw = Forwarder("http://127.0.0.1:8090/", "clave", post=post, sleep=lambda s: None)
    fw.submit("ripley", [_product("ripley-1")])
    assert fw.send_one() and fw.pending() == 0
    url, body, headers = post.calls[0]
    assert url == "http://127.0.0.1:8090/api/ingest" and headers["X-API-Key"] == "clave"
    assert body["store_id"] == "ripley" and body["products"][0]["id"] == "ripley-1"

    fw.submit("paris", [_product("paris-1", store="paris")])
    assert not fw.send_one() and fw.pending() == 1      # sin conexión: queda en espera
    assert not fw.send_one() and fw.pending() == 1      # 503: se reintenta
    assert fw.send_one() and fw.pending() == 0 and fw.sent == 2

    fw.submit("paris", [_product("paris-2", store="paris")])
    assert fw.send_one() and fw.pending() == 0 and fw.dropped == 1   # 401: reintentar no sirve
    fw.submit("ripley", [])
    assert fw.pending() == 0


def test_pipeline_forwards_each_store_read(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "f.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    pipeline.telegram_dispatcher.send_alert = MagicMock(return_value=True)
    pipeline.forwarder = MagicMock()
    a = _product("ripley-a")
    pipeline.process_store_products("ripley", [a, a], send_notifications=False)
    store_id, products = pipeline.forwarder.submit.call_args.args
    assert store_id == "ripley" and [p.id for p in products] == ["ripley-a"]


def test_ingest_endpoint_requires_key_and_processes(monkeypatch):
    from fastapi.testclient import TestClient
    import glitchhunter_ai.web.server as server

    client = TestClient(server.app)
    process = MagicMock(return_value=([], 1))
    monkeypatch.setattr(server.pipeline, "process_store_products", process)
    body = {"store_id": "ripley", "products": [
        _product("ripley-1").model_dump(mode="json"),
        _product("paris-1", store="paris").model_dump(mode="json"),   # otra tienda: se ignora
    ]}

    monkeypatch.setattr(server, "API_AUTH_KEY", "")
    assert client.post("/api/ingest", json=body).status_code == 403   # sin clave en el servidor, nunca

    monkeypatch.setattr(server, "API_AUTH_KEY", "secreta")
    assert client.post("/api/ingest", json=body).status_code == 401
    assert client.post("/api/ingest", json=body, headers={"X-API-Key": "otra"}).status_code == 401
    res = client.post("/api/ingest", json=body, headers={"X-API-Key": "secreta"})
    assert res.status_code == 200 and res.json()["received"] == 1 and res.json()["fresh"] == 1
    store_id, products = process.call_args.args
    assert store_id == "ripley" and [p.id for p in products] == ["ripley-1"]

    monkeypatch.setattr(server, "INGEST_MAX_PRODUCTS", 1)
    body["products"].append(_product("ripley-2").model_dump(mode="json"))
    assert client.post("/api/ingest", json=body, headers={"X-API-Key": "secreta"}).status_code == 413


def test_full_scan_forwards_live_store_reads(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "s.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock())
    pipeline.telegram_dispatcher.send_alert = MagicMock(return_value=True)
    scraper = MagicMock(store_id="ripley", store_name="Ripley")
    scraper.fetch_catalog.return_value = [_product("ripley-s")]
    pipeline.scrapers = [scraper]
    pipeline.forwarder = MagicMock()
    pipeline.run_scan(use_live=True, send_notifications=False, rotate_categories=False, scan_watchlist=False)
    store_id, products = pipeline.forwarder.submit.call_args.args
    assert store_id == "ripley" and products[0].id == "ripley-s"
    pipeline.forwarder.reset_mock()
    pipeline.run_scan(use_live=False, send_notifications=False, rotate_categories=False, scan_watchlist=False)
    pipeline.forwarder.submit.assert_not_called()      # datos de demostración: no se envían


def test_forwarder_persists_queue_across_restarts_and_recovers(tmp_path):
    qdir = tmp_path / "forward_queue"
    # Primera instancia: el servidor falla -> el lote queda guardado en disco
    fw1 = Forwarder("http://127.0.0.1:8090/", "clave", post=_FakePost([OSError("sin red")]), sleep=lambda s: None, queue_dir=qdir)
    fw1.submit("falabella", [_product("fal-1", store="falabella")])
    fw1.submit("ripley", [_product("rip-1", store="ripley")])
    assert not fw1.send_one()
    assert len(list(qdir.glob("*.json"))) == 2

    # Lote corrupto coexistiendo en disco: no debe trabar la recuperación
    (qdir / "99999999999999999999_999999.json").write_text("{roto", encoding="utf-8")

    # Segunda instancia tras reiniciar: recupera ambos lotes válidos y limpia al enviar / rechazar
    post2 = _FakePost([200, 413])
    fw2 = Forwarder("http://127.0.0.1:8090/", "clave", post=post2, sleep=lambda s: None, queue_dir=qdir)
    assert fw2.pending() == 2
    assert fw2.send_one() and fw2.sent == 1 and fw2.pending() == 1
    assert fw2.send_one() and fw2.dropped == 1 and fw2.pending() == 0
    assert list(qdir.glob("*.json")) == []


def test_forwarder_disk_queue_enforces_max_queue(tmp_path):
    qdir = tmp_path / "forward_queue_max"
    fw = Forwarder("http://127.0.0.1:8090/", "clave", post=_FakePost([200, 200]), sleep=lambda s: None, queue_dir=qdir, max_queue=2)
    fw.submit("paris", [_product("p-1", store="paris")])
    fw.submit("paris", [_product("p-2", store="paris")])
    fw.submit("paris", [_product("p-3", store="paris")])
    assert fw.pending() == 2 and fw.dropped == 1
    assert len(list(qdir.glob("*.json"))) == 2
    fw.send_one()
    fw.send_one()
    assert fw._post.calls[0][1]["products"][0]["id"] == "p-2"
    assert fw._post.calls[1][1]["products"][0]["id"] == "p-3"
    assert list(qdir.glob("*.json")) == []


def test_pipeline_forward_only_skips_local_db_and_alerts(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "fo.db")
    db.init_db()
    pipeline = GlitchHuntingPipeline(db=db, direct_reader=MagicMock(), forward_only=True)
    pipeline.telegram_dispatcher.send_alert = MagicMock(return_value=True)
    pipeline.forwarder = MagicMock()

    alerts, fresh = pipeline.process_store_products("ripley", [_product("rip-fo", price=19990, normal=100000)])
    assert alerts == [] and fresh == 1
    pipeline.forwarder.submit.assert_called_once()
    pipeline.telegram_dispatcher.send_alert.assert_not_called()
    assert db.get_stats()["total_products"] == 0

    # Segunda lectura con el mismo precio: no vuelve a contar como fresh
    alerts2, fresh2 = pipeline.process_store_products("ripley", [_product("rip-fo", price=19990, normal=100000)])
    assert alerts2 == [] and fresh2 == 0


def test_sync_watchlist_cache_and_collector_runner(tmp_path):
    from glitchhunter_ai.core.collector import CollectorRunner, SyncWatchlistCache

    db = PriceDatabase(db_path=tmp_path / "col.db")
    db.init_db()
    db.add_watchlist_keyword("fallback kw")

    now = [100.0]
    calls = [0]

    def fake_get(url, timeout):
        calls[0] += 1
        if calls[0] == 1:
            return {"keywords": ["rtx 5090", "lg c4"]}
        raise OSError("servidor temporalmente caído")

    cache = SyncWatchlistCache("http://127.0.0.1:8090", fallback_fn=db.get_watchlist_keywords, ttl_seconds=600.0, get_json=fake_get, clock=lambda: now[0])
    assert cache.get_keywords() == ["rtx 5090", "lg c4"]
    now[0] = 200.0
    assert cache.get_keywords() == ["rtx 5090", "lg c4"]
    assert calls[0] == 1  # servido desde caché
    now[0] = 900.0
    assert cache.get_keywords() == ["rtx 5090", "lg c4"]  # si falla remoto conserva última lista válida
    assert calls[0] == 2

    offline_cache = SyncWatchlistCache("http://127.0.0.1:8090", fallback_fn=db.get_watchlist_keywords, get_json=lambda u, t: (_ for _ in ()).throw(OSError("offline")))
    assert offline_cache.get_keywords() == ["fallback kw"]

    runner = CollectorRunner(
        forward_url="http://127.0.0.1:8090",
        forward_api_key="clave",
        queue_dir=tmp_path / "q",
        db=db,
        get_json=fake_get,
    )
    st = runner.status()
    assert st["target"] == "http://127.0.0.1:8090/api/ingest"
    assert runner.pipeline.forward_only is True


def test_forwarder_persists_latency_and_ingest_updates_store_health(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import glitchhunter_ai.web.server as server
    from glitchhunter_ai.core.store_health import StoreHealthTracker

    qdir = tmp_path / "forward_lat"
    fw1 = Forwarder("http://127.0.0.1:8090/", "clave", post=_FakePost([OSError("offline")]), sleep=lambda s: None, queue_dir=qdir)
    fw1.submit("falabella", [_product("fal-1", store="falabella")], latency_ms=345.67)
    assert not fw1.send_one()

    post2 = _FakePost([200])
    fw2 = Forwarder("http://127.0.0.1:8090/", "clave", post=post2, sleep=lambda s: None, queue_dir=qdir)
    assert fw2.send_one() and fw2.pending() == 0
    assert post2.calls[0][1]["latency_ms"] == 345.7

    tracker = StoreHealthTracker()
    monkeypatch.setattr(server, "global_store_health", tracker)
    monkeypatch.setattr(server, "API_AUTH_KEY", "secreta")
    monkeypatch.setattr(server.pipeline, "process_store_products", MagicMock(return_value=([], 1)))

    client = TestClient(server.app)
    res = client.post(
        "/api/ingest",
        json={"store_id": "falabella", "products": [_product("fal-1", store="falabella").model_dump(mode="json")], "latency_ms": 345.7},
        headers={"X-API-Key": "secreta"},
    )
    assert res.status_code == 200
    rec = next(s for s in tracker.get_matrix() if s["store_id"] == "falabella")
    assert rec["status"] == "HEALTHY"
    assert rec["latency_ms"] == 345.7
    assert rec["items_scanned"] == 1
