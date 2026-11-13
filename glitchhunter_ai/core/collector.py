"""
Modo recolector liviano para PC residencial (sin servidor web local ni escaneo duplicado).

Lee únicamente las tiendas asignadas a este equipo (RADAR_ONLY_STORES) usando la watchlist
del servidor principal y reenvía cada lectura a POST /api/ingest con cola persistente en disco.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from glitchhunter_ai.config import (
    DATA_DIR,
    DIRECT_DEEP_INTERVAL_SECONDS,
    DIRECT_DEEP_MAX_SECONDS,
    DIRECT_DEEP_MIN_SECONDS,
    DIRECT_FETCH_WORKERS,
    DIRECT_HOT_INTERVAL_SECONDS,
    DIRECT_PCFACTORY_PAGES_PER_CYCLE,
    DIRECT_SCAN_CATEGORIES,
    DIRECT_SCAN_INTERVAL_SECONDS,
    DIRECT_STORE_INTERVALS,
    EVENT_DIRECT_SCAN_INTERVAL_SECONDS,
    EVENT_MODE,
    EVENT_WINDOWS,
)
from glitchhunter_ai.core.continuous_reader import ContinuousReader
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.forwarder import Forwarder
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.core.price_signals import active_event, parse_event_windows

logger = logging.getLogger("GlitchHunter.Collector")


def _default_get_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class SyncWatchlistCache:
    """Sincroniza periódicamente las palabras clave de la watchlist desde el servidor principal."""

    def __init__(
        self,
        base_url: str,
        fallback_fn: Optional[Callable[[], List[str]]] = None,
        ttl_seconds: float = 600.0,
        get_json: Optional[Callable[[str, float], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.url = base_url.rstrip("/") + "/api/watchlist"
        self._fallback = fallback_fn or (lambda: [])
        self.ttl_seconds = float(ttl_seconds)
        self._get_json = get_json or _default_get_json
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[List[str]] = None
        self._fetched_at: float = 0.0

    def get_keywords(self) -> List[str]:
        now = self._clock()
        with self._lock:
            if self._cached is not None and (now - self._fetched_at) < self.ttl_seconds:
                return list(self._cached)
        try:
            data = self._get_json(self.url, 6.0)
            keywords = [
                str(k).strip()
                for k in (data.get("keywords") if isinstance(data, dict) else [])
                if str(k).strip()
            ]
            with self._lock:
                self._cached = keywords
                self._fetched_at = self._clock()
                return list(keywords)
        except Exception as exc:
            logger.debug("Watchlist remota no disponible (%s); usando respaldo local.", exc)
            with self._lock:
                if self._cached is not None:
                    self._fetched_at = self._clock() - max(0.0, self.ttl_seconds - 60.0)
                    return list(self._cached)
            return list(self._fallback())


def _collector_event_speed() -> float:
    windows = parse_event_windows(EVENT_WINDOWS)
    if active_event(windows, EVENT_MODE) and DIRECT_SCAN_INTERVAL_SECONDS > 0:
        return max(0.25, min(1.0, EVENT_DIRECT_SCAN_INTERVAL_SECONDS / DIRECT_SCAN_INTERVAL_SECONDS))
    return 1.0


class CollectorRunner:
    """Ejecuta solo lectores continuos + reenvío persistente sin levantar servidor web."""

    def __init__(
        self,
        forward_url: str,
        forward_api_key: str,
        queue_dir: Optional[Path] = None,
        db: Optional[PriceDatabase] = None,
        categories: Optional[List[str]] = None,
        get_json: Optional[Callable[[str, float], Any]] = None,
    ):
        if not (forward_url or "").strip():
            raise ValueError("FORWARD_TO_URL es obligatorio para ejecutar el modo recolector.")
        self.db = db or PriceDatabase()
        self.db.init_db()
        qdir = queue_dir if queue_dir is not None else (DATA_DIR / "forward_queue")
        self.forwarder = Forwarder(forward_url, forward_api_key, queue_dir=qdir)
        self.pipeline = GlitchHuntingPipeline(db=self.db, forward_only=True)
        self.pipeline.forwarder = self.forwarder
        self.watchlist_cache = SyncWatchlistCache(
            forward_url,
            fallback_fn=self.db.get_watchlist_keywords,
            get_json=get_json,
        )
        self.reader = ContinuousReader(
            self.pipeline,
            workers=DIRECT_FETCH_WORKERS,
            hot_interval=DIRECT_HOT_INTERVAL_SECONDS,
            deep_interval=DIRECT_DEEP_INTERVAL_SECONDS,
            deep_min=DIRECT_DEEP_MIN_SECONDS,
            deep_max=DIRECT_DEEP_MAX_SECONDS,
            store_intervals=DIRECT_STORE_INTERVALS,
            publish_every=60.0,
            categories=DIRECT_SCAN_CATEGORIES if categories is None else categories,
            catalog_pages=DIRECT_PCFACTORY_PAGES_PER_CYCLE,
            speed_fn=_collector_event_speed,
            watchlist_provider=self.watchlist_cache.get_keywords,
        )

    def start(self) -> "CollectorRunner":
        self.forwarder.start()
        self.reader.start()
        logger.info(
            "📡 [Recolector] Activo en segundo plano (sin web local) para %d tienda(s): %s",
            len(self.reader.stores),
            ", ".join(self.reader.stores) or "(ninguna)",
        )
        return self

    def stop(self) -> None:
        self.reader.stop()
        remaining = self.forwarder.flush(timeout=2.0)
        self.forwarder.stop()
        if remaining:
            logger.info("📦 [Recolector] %d lectura(s) quedan guardadas en disco para el próximo arranque.", remaining)
        logger.info("🛑 [Recolector] Detenido limpiamente.")

    def status(self) -> Dict[str, Any]:
        return {
            "stores": list(self.reader.stores),
            "pending": self.forwarder.pending(),
            "sent": self.forwarder.sent,
            "dropped": self.forwarder.dropped,
            "target": self.forwarder.url,
        }


def run_collector(forward_url: str, forward_api_key: str) -> None:
    """Arranca el recolector silencioso y espera señales SIGINT/SIGTERM."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    stop_event = threading.Event()
    runner = CollectorRunner(forward_url=forward_url, forward_api_key=forward_api_key)

    def _handle_signal(_signum, _frame):
        stop_event.set()

    prev_int = signal.signal(signal.SIGINT, _handle_signal)
    prev_term = signal.signal(signal.SIGTERM, _handle_signal)
    try:
        runner.start()
        while not stop_event.wait(timeout=60.0):
            st = runner.status()
            logger.info(
                "📤 [Recolector] Enviadas: %d | En espera (disco): %d | Descartadas: %d",
                st["sent"], st["pending"], st["dropped"],
            )
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        runner.stop()
