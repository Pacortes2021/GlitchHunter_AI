"""
Identificadores canónicos de producto a partir de la URL oficial de la tienda.

Un mismo producto puede llegar por el agregador (SoloTodo / Knasta) o por la lectura directa
de la tienda. Si cada fuente usara su propio ID, el radar lo trataría como dos productos:
alertas duplicadas e historial de precios partido. Derivar el ID de la URL de la tienda
garantiza que ambas fuentes coincidan.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Optional

# Falabella: /falabella-cl/product/{id}/{slug}/{sku}  (id numérico o 'prod123...')
_FALABELLA_RE = re.compile(r"/product/([A-Za-z0-9]+)(?:/|$)")
# Ripley: /{slug}-{partNumber}p  o  /{slug}-mpm{n} (marketplace)
_RIPLEY_RE = re.compile(r"-(\d{8,16})p$|-(mp[a-z]?\d{6,})$", re.IGNORECASE)


def canonical_product_id(store_id: str, url: Optional[str]) -> Optional[str]:
    """
    Retorna '{store_id}-{id_tienda}' si la URL permite identificar el producto, o None.
    Solo cubre las tiendas con lectura directa (Falabella y Ripley).
    """
    if not url:
        return None
    store = (store_id or "").lower()
    path = urllib.parse.urlparse(str(url)).path.rstrip("/")

    if store == "falabella":
        m = _FALABELLA_RE.search(path)
        return f"falabella-{m.group(1).lower()}" if m else None

    if store == "ripley":
        m = _RIPLEY_RE.search(path)
        if m:
            return f"ripley-{(m.group(1) or m.group(2)).lower()}"
        return None

    return None


def ripley_id_from_parent(parent_product_id: Optional[str]) -> Optional[str]:
    """'2000406555881P' -> 'ripley-2000406555881'; 'MPM10003498644' -> 'ripley-mpm10003498644'."""
    if not parent_product_id:
        return None
    pid = str(parent_product_id).strip().lower()
    if pid.isalnum() and pid[:-1].isdigit() and pid.endswith("p"):
        pid = pid[:-1]
    return f"ripley-{pid}" if pid else None
