"""
Buscador en vivo: consulta el buscador propio de cada tienda que lo permite (VTEX, Magento, Falabella,
Ripley, Paris, ABC, Sodimac, PC Factory) en paralelo, y no solo las ofertas que el radar ya detectó.

Cada búsqueda es un "trabajo" que avanza en segundo plano: la página consulta su progreso y va
mostrando los resultados a medida que responden las tiendas. Lo encontrado se guarda en el catálogo
y pasa por el mismo análisis del radar, así que una oferta descubierta buscando también llega al canal.
"""

import logging
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from glitchhunter_ai.scrapers.direct_reader import DIRECT_STORES, STORE_NAMES, supports_live_search

logger = logging.getLogger("GlitchHunter.Search")

JOB_TTL_SECONDS = 300        # la misma búsqueda dentro de 5 min reutiliza el resultado
MAX_ACTIVE_JOBS = 2          # búsquedas en vivo simultáneas
MAX_QUERY_LEN = 60


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def matches_query(title: str, words: List[str]) -> bool:
    """Al menos la mitad de las palabras buscadas (redondeado hacia arriba) aparecen en el título."""
    t = _norm(title)
    hits = sum(1 for w in words if w in t)
    return hits >= max(1, (len(words) + 1) // 2)


def item_to_result(item: Any, source: str = "live") -> Dict[str, Any]:
    best = float(item.best_price)
    normal = float(item.normal_price or 0)
    discount = round((normal - best) / normal * 100, 1) if normal > best > 0 else 0.0
    return {
        "product_id": item.id,
        "store_id": item.store_id,
        "store_name": STORE_NAMES.get(item.store_id, item.store_name),
        "title": item.title,
        "url": str(item.url),
        "image_url": item.image_url,
        "current_price": float(item.current_price),
        "card_price": item.card_price,
        "normal_price": normal,
        "best_price": best,
        "discount_pct": discount,
        "source": source,
    }


class LiveSearch:
    def __init__(self, pipeline: Any, workers: int = 16, clock=time.monotonic):
        self.pipeline = pipeline
        self.reader = pipeline.direct_reader
        from glitchhunter_ai.config import store_enabled
        self.stores = [s for s in DIRECT_STORES if supports_live_search(s) and store_enabled(s)]
        self.pool = ThreadPoolExecutor(max_workers=max(2, workers), thread_name_prefix="busqueda")
        self.clock = clock
        self._lock = threading.Lock()
        self.jobs: Dict[str, Dict[str, Any]] = {}

    def _purge(self) -> None:
        now = self.clock()
        for jid in [j for j, job in self.jobs.items() if job["finished_at"] and now - job["finished_at"] > JOB_TTL_SECONDS]:
            del self.jobs[jid]

    def start(self, query: str) -> Optional[Dict[str, Any]]:
        """Inicia (o reutiliza) una búsqueda en vivo. None si ya hay demasiadas en curso."""
        query = " ".join((query or "").split())[:MAX_QUERY_LEN]
        words = [w for w in _norm(query).split() if len(w) >= 2]
        if not words:
            raise ValueError("Búsqueda vacía")
        key = " ".join(words)
        with self._lock:
            self._purge()
            existing = next((j["id"] for j in self.jobs.values() if j["key"] == key), None)
            if existing is None and sum(1 for j in self.jobs.values() if not j["finished_at"]) >= MAX_ACTIVE_JOBS:
                return None
        if existing is not None:
            return self.snapshot(existing)
        with self._lock:
            job = {
                "id": uuid.uuid4().hex[:12], "key": key, "query": query, "words": words,
                "stores": list(self.stores), "done": 0, "failed": [], "results": {}, "by_store": {},
                "started_at": self.clock(), "finished_at": None,
            }
            self.jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job,), name=f"busqueda-{job['id']}", daemon=True).start()
        return self.snapshot(job["id"])

    def _search_store(self, job: Dict[str, Any], store_id: str) -> None:
        items: List[Any] = []
        try:
            items = [i for i in self.reader.search(store_id, job["query"]) if matches_query(i.title, job["words"])]
        except Exception as exc:
            logger.debug("Búsqueda en %s falló: %s", store_id, exc)
            with self._lock:
                job["failed"].append(store_id)
        with self._lock:
            for item in items:
                job["results"].setdefault(item.id, item_to_result(item))
            job["by_store"][store_id] = items
            job["done"] += 1

    def _run(self, job: Dict[str, Any]) -> None:
        futures = [self.pool.submit(self._search_store, job, s) for s in job["stores"]]
        for fut in futures:
            fut.result()
        with self._lock:
            job["finished_at"] = self.clock()
            by_store = dict(job["by_store"])
        found = sum(len(v) for v in by_store.values())
        logger.info("🔎 [Buscador] \"%s\": %d producto(s) en %d tienda(s) (%.0f s).", job["query"], found,
                    sum(1 for v in by_store.values() if v), job["finished_at"] - job["started_at"])
        # Lo encontrado entra al catálogo y al análisis del radar (puede publicar ofertas nuevas)
        for store_id, items in by_store.items():
            if items:
                try:
                    self.pipeline.process_store_products(store_id, items, lock_timeout=300)
                except Exception as exc:
                    logger.warning("⚠️ [Buscador] no se pudo guardar lo encontrado en %s: %s", store_id, exc)

    def snapshot(self, job_id: str, limit: int = 300) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            results = sorted(job["results"].values(), key=lambda r: (-r["discount_pct"], r["best_price"]))
            with_results = sum(1 for v in job["by_store"].values() if v)
            done = job["done"]
        return {
            "id": job["id"],
            "query": job["query"],
            "total_stores": len(job["stores"]),
            "done_stores": done,
            "stores_with_results": with_results,
            "finished": job["finished_at"] is not None,
            "count": len(results),
            "results": results[:limit],
        }
