"""
Señales de precio basadas en la experiencia histórica chilena (36 casos documentados y
28 fallos de Cortes de Apelaciones analizados por la UDP, 2007-2024):

- Nivel de riesgo según el descuento: los precios "irrisorios" (~95% o más de descuento,
  ej: una cama a $120) casi siempre se anulan y los tribunales validan la anulación;
  la "zona plausible" (~60%-95%) concentra los casos respetados o compensados,
  sobre todo durante eventos como CyberDay.
- Cifra inusual: el retail chileno fija precios terminados en ...90/...00 (990, 490…).
  Los errores de cálculo de porcentajes dejan cifras arbitrarias ($9.710, $5.872, $179.991).
- Carga masiva: varios productos distintos de una tienda al MISMO precio, con grandes descuentos
  sobre precios normales diferentes (ej: Cencosud vendió camas y sets de dormitorio, todos a
  $38.990), delatan un error al cargar el catálogo.
- Modo evento: la mayoría de los errores ocurre en CyberDay, Cyber Monday, Black Friday y
  Navidad, y dura de minutos a 2 horas, así que durante esos días se escanea más seguido.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

logger = logging.getLogger("glitchhunter.core.price_signals")

RISK_IRRISORIO = "irrisorio"
RISK_PLAUSIBLE = "plausible"

IRRISORIO_MIN_DISCOUNT = 95.0
PLAUSIBLE_MIN_DISCOUNT = 60.0
ODD_PRICE_MIN = 20000.0
_USUAL_ENDINGS = {0, 90, 50, 95, 99}   # últimas dos cifras de precios fijados a mano
# En marketplaces cada vendedor fija precios arbitrarios ($104.982), y algunas tiendas calculan sus
# precios en vez de fijarlos (Winpy: $367.840; HP: $825.220): ahí la terminación no indica nada
MARKETPLACE_STORES = {"mercadolibre"}
COMPUTED_PRICE_STORES = {"winpy", "hp"}


def risk_tier(discount_pct: Optional[float]) -> Optional[str]:
    """'irrisorio' (≥95%), 'plausible' (60%-95%) o None (descuento comercial)."""
    if discount_pct is None:
        return None
    d = float(discount_pct)
    if d >= IRRISORIO_MIN_DISCOUNT:
        return RISK_IRRISORIO
    if d >= PLAUSIBLE_MIN_DISCOUNT:
        return RISK_PLAUSIBLE
    return None


def is_odd_price(price: Optional[float], store_id: Optional[str] = None) -> bool:
    """True si un precio relevante no termina como los precios fijados por el retail (…90, …00)."""
    if (store_id or "").lower() in MARKETPLACE_STORES | COMPUTED_PRICE_STORES:
        return False
    if not price or float(price) < ODD_PRICE_MIN:
        return False
    cents = int(round(float(price))) % 100
    return cents not in _USUAL_ENDINGS


BULK_MIN_PRODUCTS = 4
BULK_MIN_DISCOUNT = 60.0
BULK_MIN_DISTINCT_NORMALS = 3


def detect_bulk_prices(products: Iterable[Any]) -> Dict[str, int]:
    """
    Productos que comparten precio exacto con otros de la misma tienda en un mismo escaneo
    (≥4 productos distintos, ≥60% de descuento, ≥3 precios normales distintos).
    Retorna {product_id: cantidad de productos con ese mismo precio}.
    """
    groups: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for p in products:
        try:
            best = float(p.best_price)
            normal = float(p.normal_price or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if best <= 0 or normal <= 0 or float(p.discount_percentage) < BULK_MIN_DISCOUNT:
            continue
        key = (str(p.store_id), int(round(best)))
        groups.setdefault(key, {})[str(p.id)] = int(round(normal))
    result: Dict[str, int] = {}
    for members in groups.values():
        if len(members) >= BULK_MIN_PRODUCTS and len(set(members.values())) >= BULK_MIN_DISTINCT_NORMALS:
            for pid in members:
                result[pid] = len(members)
    return result


@dataclass(frozen=True)
class EventWindow:
    name: str
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def parse_event_windows(raw: str) -> List[EventWindow]:
    """
    Formato: "Nombre=AAAA-MM-DD:AAAA-MM-DD, Otro=AAAA-MM-DD:AAAA-MM-DD" (fechas inclusive).
    El nombre es opcional. Entradas inválidas se ignoran con un aviso en el log.
    """
    windows: List[EventWindow] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, span = chunk.rpartition("=")
        try:
            start_s, end_s = span.split(":")
            start, end = date.fromisoformat(start_s.strip()), date.fromisoformat(end_s.strip())
        except ValueError:
            logger.warning(f"Ventana de evento inválida ignorada: '{chunk}' (formato Nombre=AAAA-MM-DD:AAAA-MM-DD)")
            continue
        if end < start:
            start, end = end, start
        windows.append(EventWindow(name=name.strip() or "Evento", start=start, end=end))
    return windows


def chile_now() -> datetime:
    """Hora local de Chile (los eventos y horarios de carga de catálogo son locales)."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/Santiago"))
        except Exception:
            pass
    return datetime.now()


def active_event(windows: List[EventWindow], mode: str = "auto", now: Optional[datetime] = None) -> Optional[str]:
    """
    Nombre del evento activo, o None. mode: 'auto' (según ventanas), 'on' (siempre), 'off' (nunca).
    """
    mode = (mode or "auto").strip().lower()
    if mode == "off":
        return None
    if mode == "on":
        return "Modo evento manual"
    today = (now or chile_now()).date()
    for w in windows:
        if w.contains(today):
            return w.name
    return None
