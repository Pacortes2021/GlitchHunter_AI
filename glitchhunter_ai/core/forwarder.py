"""
Reenvío de lecturas a otro radar (p. ej. del PC de la casa al servidor en la nube).

Algunas tiendas bloquean las IPs de centros de datos, así que el PC las lee y el servidor no.
Para que todo termine en un solo panel, el PC envía cada lectura (los productos de una tienda)
al endpoint POST /api/ingest del servidor, que la procesa con su propio historial y la publica
en su canal. El envío corre en un hilo aparte: si el servidor no responde, las lecturas esperan
en una cola acotada y se reintentan; nunca frenan a los lectores.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger("GlitchHunter.Forwarder")
_HTTP_CLIENT: Any = None
_HTTP_CLIENT_LOCK = threading.Lock()
MAX_COALESCE_PRODUCTS = 1500

INGEST_PATH = "/api/ingest"
MAX_QUEUE = 500          # lecturas en espera; si se llena se descarta la más antigua
MAX_BACKOFF = 60.0       # espera máxima entre reintentos cuando el servidor no responde


def _unlink_file(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class Forwarder:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 45.0,
        post: Optional[Callable[[str, bytes, dict, float], int]] = None,
        sleep: Callable[[float], None] = time.sleep,
        queue_dir: Optional[Union[str, Path]] = None,
        max_queue: int = MAX_QUEUE,
    ):
        self.url = base_url.rstrip("/") + INGEST_PATH
        self.api_key = api_key
        self.timeout = timeout
        self._post = post or _urllib_post
        self._sleep = sleep
        self.queue_dir: Optional[Path] = Path(queue_dir) if queue_dir else None
        self._max_queue = max(1, int(max_queue))
        self._seq = 0
        self._queue: Deque[Tuple[str, List[dict], Optional[float], Optional[Path]]] = deque()
        self._cond = threading.Condition()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self.sent = 0
        self.dropped = 0
        self._failing_since: Optional[float] = None
        if self.queue_dir is not None:
            self._load_queue()

    def _load_queue(self) -> None:
        """Restaura lecturas pendientes guardadas en disco ante reinicios o desconexiones."""
        if self.queue_dir is None:
            return
        try:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("⚠️ [Reenvío] No se pudo crear cola persistente en %s: %s", self.queue_dir, exc)
            return
        for tmp in self.queue_dir.glob("*.tmp"):
            _unlink_file(tmp)
        loaded: List[Tuple[str, List[dict], Optional[float], Path]] = []
        for path in sorted(self.queue_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                store_id = data.get("store_id") if isinstance(data, dict) else None
                products = data.get("products") if isinstance(data, dict) else None
                if not isinstance(store_id, str) or not store_id or not isinstance(products, list):
                    raise ValueError("formato inválido")
                raw_lat = data.get("latency_ms")
                latency_ms = round(float(raw_lat), 1) if isinstance(raw_lat, (int, float)) and raw_lat >= 0 else None
                loaded.append((store_id, products, latency_ms, path))
            except Exception as exc:
                logger.warning("⚠️ [Reenvío] Se descartó lote corrupto en %s (%s).", path.name, exc)
                _unlink_file(path)
        if len(loaded) > self._max_queue:
            excess = len(loaded) - self._max_queue
            for _, _, _, old_path in loaded[:excess]:
                _unlink_file(old_path)
            self.dropped += excess
            loaded = loaded[excess:]
        self._queue.extend(loaded)
        if loaded:
            logger.info("📦 [Reenvío] %d lectura(s) pendiente(s) recuperada(s) desde disco.", len(loaded))

    def _persist_batch(
        self,
        store_id: str,
        payload: List[dict],
        latency_ms: Optional[float] = None,
    ) -> Optional[Path]:
        """Guarda un lote en disco de forma atómica (.tmp -> replace)."""
        if self.queue_dir is None:
            return None
        try:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            filename = f"{time.time_ns():020d}_{self._seq:06d}.json"
            target = self.queue_dir / filename
            tmp = self.queue_dir / f".{filename}.tmp"
            record: dict = {"store_id": store_id, "products": payload}
            if latency_ms is not None:
                record["latency_ms"] = round(float(latency_ms), 1)
            tmp.write_text(
                json.dumps(record, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, target)
            return target
        except OSError as exc:
            logger.warning("⚠️ [Reenvío] No se pudo guardar lote en disco (%s); sigue en memoria.", exc)
            return None

    def submit(self, store_id: str, products: List[Any], latency_ms: Optional[float] = None) -> None:
        """Encola una lectura (no bloquea). Acepta ProductItem o dicts."""
        if not products:
            return
        norm_lat = round(float(latency_ms), 1) if latency_ms is not None and latency_ms >= 0 else None
        payload = [p.model_dump(mode="json") if hasattr(p, "model_dump") else dict(p) for p in products]
        with self._cond:
            while len(self._queue) >= self._max_queue:
                _, _, _, old_path = self._queue.popleft()
                _unlink_file(old_path)
                self.dropped += 1
            batch_path = self._persist_batch(store_id, payload, latency_ms=norm_lat)
            self._queue.append((store_id, payload, norm_lat, batch_path))
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def _collect_send_batch_unlocked(self) -> Tuple[str, List[dict], Optional[float], List[Tuple[str, List[dict], Optional[float], Optional[Path]]]]:
        """Prepara el siguiente lote a enviar; con >=4 en cola fusiona lecturas de la misma tienda."""
        first_entry = self._queue[0]
        first_store, first_products, first_latency, _ = first_entry
        if len(self._queue) < 4:
            return first_store, first_products, first_latency, [first_entry]
        merged_by_id: Dict[str, dict] = {}
        extra_no_id: List[dict] = []
        batch_entries: List[Tuple[str, List[dict], Optional[float], Optional[Path]]] = []
        latest_latency = first_latency
        for entry in self._queue:
            sid, prods, lat, _ = entry
            if sid != first_store:
                continue
            projected = len(merged_by_id) + len(extra_no_id) + len(prods)
            if batch_entries and projected > MAX_COALESCE_PRODUCTS:
                break
            batch_entries.append(entry)
            for p in prods:
                pid = p.get("id") if isinstance(p, dict) else None
                if pid:
                    merged_by_id[str(pid)] = p
                else:
                    extra_no_id.append(p)
            if lat is not None:
                latest_latency = lat
        combined = list(merged_by_id.values()) + extra_no_id
        return first_store, combined, latest_latency, batch_entries

    def _drop_sent_entries(self, batch_entries: List[Tuple[str, List[dict], Optional[float], Optional[Path]]]) -> int:
        batch_ids = {id(entry) for entry in batch_entries}
        removed_count = 0
        with self._cond:
            remaining: Deque[Tuple[str, List[dict], Optional[float], Optional[Path]]] = deque()
            for entry in self._queue:
                if id(entry) in batch_ids:
                    _unlink_file(entry[3])
                    removed_count += 1
                else:
                    remaining.append(entry)
            self._queue = remaining
        return removed_count

    def send_one(self) -> bool:
        """Envía la lectura más antigua (o fusión de la misma tienda si hay cola). True si salió bien."""
        with self._cond:
            if not self._queue:
                return True
            store_id, products, latency_ms, batch_entries = self._collect_send_batch_unlocked()
        body_dict: dict = {"store_id": store_id, "products": products}
        if latency_ms is not None:
            body_dict["latency_ms"] = latency_ms
        body = json.dumps(body_dict).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-API-Key": self.api_key}
        try:
            code = self._post(self.url, body, headers, self.timeout)
        except Exception as exc:   # sin conexión, túnel caído, timeout
            code, err = 0, exc
        else:
            err = None
        if 200 <= code < 300:
            cleared = self._drop_sent_entries(batch_entries)
            self.sent += cleared
            if self._failing_since is not None:
                logger.info("🔗 [Reenvío] Conexión con el servidor recuperada (%d lectura(s) en espera).", self.pending())
                self._failing_since = None
            return True
        if code in (400, 401, 403, 413, 422):
            # El servidor rechaza esta lectura (clave inválida, formato): reintentarla no sirve
            cleared = self._drop_sent_entries(batch_entries)
            self.dropped += cleared
            logger.warning("⚠️ [Reenvío] El servidor rechazó la lectura de %s (HTTP %s).", store_id, code)
            return True
        if self._failing_since is None:
            self._failing_since = time.monotonic()
            logger.warning("⚠️ [Reenvío] El servidor no responde (%s); las lecturas quedan en espera.", err or f"HTTP {code}")
        return False

    def flush(self, timeout: float = 3.0) -> int:
        """Espera brevemente a vaciar la cola al cerrar si el hilo sigue conectado y enviando."""
        if self._thread is None or not self._thread.is_alive():
            return self.pending()
        deadline = time.monotonic() + max(0.0, timeout)
        while self.pending() > 0 and self._failing_since is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.pending()

    def _loop(self) -> None:
        backoff = 2.0
        while True:
            with self._cond:
                while not self._queue and not self._stop:
                    self._cond.wait(timeout=5.0)
                if self._stop:
                    return
            if self.send_one():
                backoff = 2.0
            else:
                self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    def start(self) -> "Forwarder":
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="reenvio", daemon=True)
            self._thread.start()
            logger.info("🔗 [Reenvío] Las lecturas de este radar se envían a %s.", self.url)
        return self

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()


def _get_http_client() -> Any:
    global _HTTP_CLIENT
    if httpx is None:
        return None
    if _HTTP_CLIENT is None:
        with _HTTP_CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.Client(
                    limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
                    timeout=45.0,
                )
    return _HTTP_CLIENT


def _urllib_post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    client = _get_http_client()
    if client is not None:
        resp = client.post(url, content=body, headers=headers, timeout=timeout)
        return resp.status_code
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
