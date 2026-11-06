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
