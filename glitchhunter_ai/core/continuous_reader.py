"""
Lectores continuos por tienda.

Reemplaza el "escaneo directo" por ciclos (todas las tiendas juntas y luego una pausa) por lectores
independientes: cada tienda se lee cuando le toca según su propio ritmo (core/scheduler.py), en un
grupo de hilos compartido. Una tienda lenta ya no atrasa a las demás, y lo que trae una lectura se
procesa apenas llega.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional

from glitchhunter_ai.core.scheduler import DEEP, HOT, StoreScheduler, build_scheduler, parse_interval_overrides
from glitchhunter_ai.scrapers.direct_reader import CATALOG_STORES, DIRECT_STORES, HOT_LANE_STORES, SHOPIFY_STORES

logger = logging.getLogger("GlitchHunter.Readers")

SHOPIFY_KEY = "shopify"
SHOPIFY_LANE = "turno"
SHOPIFY_POLL_SECONDS = 10.0   # fetch_shopify_next respeta su propio espaciado global


class ContinuousReader:
    def __init__(
        self,
        pipeline: Any,
        workers: int,
        hot_interval: float,
        deep_interval: float,
        deep_min: float,
        deep_max: float,
        store_intervals: str = "",
        publish_every: float = 20.0,
        categories: Optional[List[str]] = None,
        catalog_pages: int = 10,
        speed_fn: Optional[Callable[[], float]] = None,
        on_published: Optional[Callable[[int], Any]] = None,
        store_filter: Optional[Callable[[str], bool]] = None,
    ):
        self.pipeline = pipeline
        self.reader = pipeline.direct_reader
        self.categories = categories or []
        self.catalog_pages = catalog_pages
        self.speed_fn = speed_fn or (lambda: 1.0)
        self.on_published = on_published
        self.publish_every = publish_every
        # Reparto entre radares (RADAR_ONLY_STORES / RADAR_SKIP_STORES): este radar solo lee las suyas
        if store_filter is None:
            from glitchhunter_ai.config import store_enabled as store_filter
        enabled = [s for s in DIRECT_STORES if store_filter(s)]
        self.stores = enabled
        hot_stores = [s for s in HOT_LANE_STORES if s in enabled]
        deep_stores = [s for s in enabled if s not in SHOPIFY_STORES]
        shops = [s for s in SHOPIFY_STORES if s in enabled]
        self.reader.shopify_stores = shops
        self.scheduler: StoreScheduler = build_scheduler(
            hot_stores, deep_stores, hot_interval, deep_interval, deep_min, deep_max,
            overrides=parse_interval_overrides(store_intervals),
        )
        if shops:
            self.scheduler.add(SHOPIFY_KEY, SHOPIFY_LANE, SHOPIFY_POLL_SECONDS)
        self.pool = ThreadPoolExecutor(max_workers=max(2, workers), thread_name_prefix="lector")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stats_lock = threading.Lock()
        self._minute = {"reads": 0, "products": 0, "fresh": 0, "alerts": 0}

    # --- lectura de un carril ------------------------------------------------------
    def _fetch(self, store_id: str, lane: str) -> List:
        if lane == HOT:
            return self.reader.fetch_hot(store_id)
        items: List = []
        if store_id not in CATALOG_STORES:
            # Tiendas sin recorrido de catálogo: watchlist y categorías prioritarias
            for kw in self.pipeline.db.get_watchlist_keywords():
                items.extend(self.reader.search(store_id, kw))
            for cat in self.categories:
                items.extend(self.reader.fetch_category(store_id, cat))
            return items
        return self.reader.fetch_deep(store_id, self.catalog_pages)

    def _process(self, store_id: str, items: List) -> tuple:
        alerts, fresh = self.pipeline.process_store_products(store_id, items)
        with self._stats_lock:
            self._minute["reads"] += 1
            self._minute["products"] += len(items)
            self._minute["fresh"] += fresh
            self._minute["alerts"] += len(alerts)
        return alerts, fresh

    def run_lane(self, store_id: str, lane: str) -> None:
        count, fresh, ok = 0, 0, True
        try:
            if store_id == SHOPIFY_KEY:
                result = self.reader.fetch_shopify_next()
                if result:
                    shop, _, items = result
                    count = len(items)
                    _, fresh = self._process(shop, items)
                return
            items = self._fetch(store_id, lane)
            count = len(items)
            if items:
                _, fresh = self._process(store_id, items)
        except Exception as exc:
            ok = False
            logger.warning("⚠️ [Lector %s/%s] falló: %s", store_id, lane, exc)
        finally:
            self.scheduler.done(store_id, lane, count, fresh, ok=ok)

    # --- bucle -------------------------------------------------------------------
    def _loop(self) -> None:
        last_publish = last_stats = time.monotonic()
        publishing = None
        while not self._stop.is_set():
            try:
                self.scheduler.speed = self.speed_fn()
                for store_id, lane in self.scheduler.due():
                    self.pool.submit(self.run_lane, store_id, lane)
                now = time.monotonic()
                if now - last_publish >= self.publish_every and (publishing is None or publishing.done()):
                    last_publish = now
                    publishing = self.pool.submit(self._publish)
                if now - last_stats >= 60:
                    last_stats = now
                    self._log_minute()
            except Exception as exc:
                logger.error("❌ Error en el planificador de lectores: %s", exc)
            self._stop.wait(1.0)

    def _publish(self) -> None:
        try:
            published = self.pipeline.publish_tick()
            if published and self.on_published:
                self.on_published(published)
        except Exception as exc:
            logger.warning("⚠️ No se pudo publicar en el canal: %s", exc)

    def _log_minute(self) -> None:
        with self._stats_lock:
            m, self._minute = self._minute, {"reads": 0, "products": 0, "fresh": 0, "alerts": 0}
        logger.info(
            "⚡ [Lectores] último minuto: %d lectura(s), %d producto(s), %d oferta(s) nueva(s) o más baratas, %d alerta(s).",
            m["reads"], m["products"], m["fresh"], m["alerts"],
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="planificador-lectores", daemon=True)
        self._thread.start()
        hot = sum(1 for (_, lane) in self.scheduler.lanes if lane == HOT)
        deep = sum(1 for (_, lane) in self.scheduler.lanes if lane == DEEP)
        logger.info(
            "⚡ Lectores por tienda activos: %d carriles rápidos (cada %ds), %d recorridos (ritmo base %ds, "
            "adaptativo %d-%ds) y %d tiendas Shopify por turnos.",
            hot, self.scheduler.hot_interval, deep, self.scheduler.deep_interval,
            self.scheduler.deep_min, self.scheduler.deep_max, len(self.reader.shopify_stores or []),
        )

    def stop(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
