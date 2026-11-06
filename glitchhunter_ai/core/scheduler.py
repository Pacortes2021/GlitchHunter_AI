"""
Ritmo de lectura por tienda.

Cada tienda tiene hasta dos carriles independientes:
- "hot" (carril rápido): una sola petición a lo que más cambia (ofertas, mejor descuento). Ritmo fijo y corto.
- "deep" (recorrido): las siguientes páginas del catálogo completo. Ritmo adaptativo: se acorta cuando
  la tienda trae ofertas nuevas, vuelve de a poco al ritmo base cuando no, y se alarga si no responde.

Ya no hay un ciclo común: una tienda lenta no atrasa a las demás.
"""

import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

HOT = "hot"
DEEP = "deep"


@dataclass
class LaneState:
    store_id: str
    lane: str
    base: float
    interval: float
    next_at: float
    in_flight: bool = False
    last_started: Optional[float] = None
    last_finished: Optional[float] = None
    last_count: int = 0
    last_fresh: int = 0
    total_fresh: int = 0
    reads: int = 0
    failures: int = 0


@dataclass
class StoreScheduler:
    """Planificador de lecturas: due() entrega lo que toca leer y done() registra el resultado."""

    hot_interval: float = 60.0
    deep_interval: float = 180.0
    deep_min: float = 60.0
    deep_max: float = 900.0
    clock: Callable[[], float] = time.monotonic
    speed: float = 1.0   # < 1 acelera todo (modo evento)
    lanes: Dict[Tuple[str, str], LaneState] = field(default_factory=dict)

    def __post_init__(self):
        self._lock = threading.Lock()

    def add(self, store_id: str, lane: str, base: Optional[float] = None) -> None:
        """Agrega un carril. La primera lectura se reparte dentro del intervalo para no partir todas juntas."""
        base = float(base if base is not None else (self.hot_interval if lane == HOT else self.deep_interval))
        spread = (zlib.crc32(f"{store_id}:{lane}".encode()) % 1000) / 1000.0
        first = self.clock() + spread * min(base, 30.0)
        self.lanes[(store_id, lane)] = LaneState(store_id, lane, base, base, first)

    def due(self) -> List[Tuple[str, str]]:
        """Carriles cuyo turno llegó (y que no están en lectura). Quedan marcados en lectura."""
        now = self.clock()
        out = []
        with self._lock:
            for key, st in self.lanes.items():
                if not st.in_flight and now >= st.next_at:
                    st.in_flight = True
                    st.last_started = now
                    out.append(key)
        out.sort(key=lambda k: self.lanes[k].next_at)
        return out

    def done(self, store_id: str, lane: str, count: int, fresh: int, ok: bool = True) -> None:
        """
        Registra una lectura. fresh = ofertas nuevas o que bajaron de precio en esta lectura.
        El próximo turno se cuenta desde el inicio de esta lectura (inicio a inicio).
        """
        now = self.clock()
        with self._lock:
            st = self.lanes.get((store_id, lane))
            if st is None:
                return
            st.in_flight = False
            st.last_finished = now
            st.last_count = count
            st.last_fresh = fresh
            st.total_fresh += fresh
            st.reads += 1
            if not ok:
                st.failures += 1
            if lane == DEEP:
                if fresh > 0:
                    st.interval = max(self.deep_min, st.interval / 2)          # tienda movida: más seguido
                elif not ok or count == 0:
                    st.interval = min(self.deep_max, st.interval * 2)          # no responde: más espaciado
                else:
                    st.interval += (st.base - st.interval) * 0.25              # vuelve de a poco al ritmo base
            start = st.last_started if st.last_started is not None else now
            st.next_at = max(now + 1.0, start + st.interval * self.speed)

    def snapshot(self) -> List[Dict]:
        now = self.clock()
        with self._lock:
            rows = []
            for st in self.lanes.values():
                rows.append({
                    "store_id": st.store_id,
                    "lane": st.lane,
                    "interval_s": round(st.interval * self.speed),
                    "next_in_s": max(0, round(st.next_at - now)),
                    "reading": st.in_flight,
                    "last_read_ago_s": None if st.last_finished is None else round(now - st.last_finished),
                    "last_count": st.last_count,
                    "last_fresh": st.last_fresh,
                    "total_fresh": st.total_fresh,
                    "reads": st.reads,
                    "failures": st.failures,
                })
        rows.sort(key=lambda r: (r["store_id"], r["lane"]))
        return rows


def parse_interval_overrides(raw: str) -> Dict[str, float]:
    """'gap=60, falabella=90' -> {'gap': 60.0, 'falabella': 90.0} (ignora entradas mal formadas)."""
    out: Dict[str, float] = {}
    for part in (raw or "").split(","):
        key, _, val = part.partition("=")
        try:
            if key.strip() and float(val) > 0:
                out[key.strip()] = float(val)
        except ValueError:
            continue
    return out


def build_scheduler(
    hot_stores: Iterable[str],
    deep_stores: Iterable[str],
    hot_interval: float,
    deep_interval: float,
    deep_min: float,
    deep_max: float,
    overrides: Optional[Dict[str, float]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> StoreScheduler:
    sched = StoreScheduler(hot_interval=hot_interval, deep_interval=deep_interval,
                           deep_min=deep_min, deep_max=deep_max, clock=clock)
    overrides = overrides or {}
    for s in hot_stores:
        sched.add(s, HOT)
    for s in deep_stores:
        sched.add(s, DEEP, overrides.get(s))
    return sched
