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
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Deque, List, Optional, Tuple

logger = logging.getLogger("GlitchHunter.Forwarder")

INGEST_PATH = "/api/ingest"
MAX_QUEUE = 500          # lecturas en espera; si se llena se descarta la más antigua
MAX_BACKOFF = 60.0       # espera máxima entre reintentos cuando el servidor no responde


class Forwarder:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        post: Optional[Callable[[str, bytes, dict, float], int]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.url = base_url.rstrip("/") + INGEST_PATH
        self.api_key = api_key
        self.timeout = timeout
        self._post = post or _urllib_post
        self._sleep = sleep
        self._queue: Deque[Tuple[str, List[dict]]] = deque()
        self._cond = threading.Condition()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self.sent = 0
        self.dropped = 0
        self._failing_since: Optional[float] = None

    def submit(self, store_id: str, products: List[Any]) -> None:
        """Encola una lectura (no bloquea). Acepta ProductItem o dicts."""
        if not products:
            return
        payload = [p.model_dump(mode="json") if hasattr(p, "model_dump") else dict(p) for p in products]
        with self._cond:
            if len(self._queue) >= MAX_QUEUE:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append((store_id, payload))
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def send_one(self) -> bool:
        """Envía la lectura más antigua. True si salió bien (o no había nada); False si hay que reintentar."""
        with self._cond:
            if not self._queue:
                return True
            store_id, products = self._queue[0]
        body = json.dumps({"store_id": store_id, "products": products}).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-API-Key": self.api_key}
        try:
            code = self._post(self.url, body, headers, self.timeout)
        except Exception as exc:   # sin conexión, túnel caído, timeout
            code, err = 0, exc
        else:
            err = None
        if 200 <= code < 300:
            with self._cond:
                if self._queue and self._queue[0][1] is products:
                    self._queue.popleft()
            self.sent += 1
            if self._failing_since is not None:
                logger.info("🔗 [Reenvío] Conexión con el servidor recuperada (%d lectura(s) en espera).", self.pending())
                self._failing_since = None
            return True
        if code in (400, 401, 403, 413, 422):
            # El servidor rechaza esta lectura (clave inválida, formato): reintentarla no sirve
            with self._cond:
                if self._queue and self._queue[0][1] is products:
                    self._queue.popleft()
            self.dropped += 1
            logger.warning("⚠️ [Reenvío] El servidor rechazó la lectura de %s (HTTP %s).", store_id, code)
            return True
        if self._failing_since is None:
            self._failing_since = time.monotonic()
            logger.warning("⚠️ [Reenvío] El servidor no responde (%s); las lecturas quedan en espera.", err or f"HTTP {code}")
        return False

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


def _urllib_post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
