"""
Matriz de Salud y Latencia de Tiendas (Store Health & Latency Matrix).
Monitorea tiempos de respuesta, tasa de disponibilidad y salud operativa
de cada scraper en tiempo real.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class StoreHealthTracker:
    """Rastreador de métricas operacionales por retailer."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stores: Dict[str, Dict[str, Any]] = {}

    def register_store(self, store_id: str, store_name: str):
        with self._lock:
            if store_id not in self._stores:
                self._stores[store_id] = {
                    "store_id": store_id,
                    "store_name": store_name,
                    "status": "IDLE",  # HEALTHY, DEGRADED, OFFLINE, IDLE
                    "latency_ms": 0.0,
                    "avg_latency_ms": 0.0,
                    "items_scanned": 0,
                    "glitches_detected": 0,
                    "error_count": 0,
                    "success_scans": 0,
                    "last_scan_at": None,
                }

    def record_scan(
        self,
        store_id: str,
        store_name: str,
        latency_ms: float,
        items_count: int,
        error: bool = False,
        blocked: bool = False,
    ):
        with self._lock:
            self.register_store(store_id, store_name)
            record = self._stores[store_id]
            record["last_scan_at"] = datetime.now(timezone.utc).isoformat()
            record["latency_ms"] = round(latency_ms, 1)

            if error:
                record["error_count"] += 1
                record["status"] = "OFFLINE" if record["error_count"] >= 3 else "DEGRADED"
            elif blocked:
                record["error_count"] += 1
                record["status"] = "BLOCKED"
            elif items_count == 0:
                # Scraper respondió sin excepción pero no obtuvo productos (evita falsos positivos - Hallazgo D de Auditoría 4)
                record["status"] = "EMPTY_RESPONSE"
            else:
                record["success_scans"] += 1
                record["items_scanned"] += items_count
                record["error_count"] = 0
                if latency_ms > 4000.0:
                    record["status"] = "DEGRADED"
                else:
                    record["status"] = "HEALTHY"

                # Calcular media móvil ponderada de latencia
                if record["avg_latency_ms"] == 0.0:
                    record["avg_latency_ms"] = record["latency_ms"]
                else:
                    record["avg_latency_ms"] = round(
                        record["avg_latency_ms"] * 0.7 + record["latency_ms"] * 0.3, 1
                    )

    def get_store_latency(self, store_id: str) -> Optional[float]:
        with self._lock:
            store = self._stores.get(store_id)
            if store and store.get("latency_ms", 0.0) > 0.0:
                return store["latency_ms"]
            return None

    def record_glitch(self, store_id: str):
        with self._lock:
            if store_id in self._stores:
                self._stores[store_id]["glitches_detected"] += 1

    def get_matrix(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in self._stores.values()]


# Singleton global de salud de tiendas
global_store_health = StoreHealthTracker()
