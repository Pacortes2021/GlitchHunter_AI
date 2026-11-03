"""
Motor de Extracción en Vivo de Retail Chileno (Live Real Feeder).
Obtiene productos reales, actuales y verificados con stock en tiempo real
desde los 6 principales retailers de Chile:
- Falabella
- Ripley
- Paris
- Líder
- Mercado Libre Chile
- PC Factory

Garantiza que el 100% de los links generados sean URLs oficiales y vivas de las tiendas.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import urllib.parse

import requests

from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.scrapers.product_ids import canonical_product_id
from glitchhunter_ai.scrapers.stealth_client import default_stealth_client

logger = logging.getLogger("glitchhunter.scrapers.live_feed")

STORE_MAPPING: Dict[str, Dict[str, Any]] = {
    "falabella": {
        "solotodo_id": 9,
        "name": "Falabella Chile",
        "knasta_name": "falabella",
        "domain": "falabella.com",
    },
    "ripley": {
        "solotodo_id": 18,
        "name": "Ripley Chile",
        "knasta_name": "ripley",
        "domain": "simple.ripley.cl",
    },
    "paris": {
        "solotodo_id": 11,
        "name": "Paris.cl",
        "knasta_name": "paris",
        "domain": "paris.cl",
    },
    "lider": {
        "solotodo_id": 43,
        "name": "Líder Chile",
        "knasta_name": "lider",
        "domain": "lider.cl",
    },
    "mercadolibre": {
        "solotodo_id": 260,
        "name": "Mercado Libre Chile",
        "knasta_name": "mercadolibre",
        "domain": "mercadolibre.cl",
    },
    "pcfactory": {
        "solotodo_id": 12,
        "name": "PC Factory",
        "knasta_name": "pcfactory",
        "domain": "pcfactory.cl",
    },
}

KNASTA_TO_STORE_ID: Dict[str, str] = {
    v["knasta_name"]: k for k, v in STORE_MAPPING.items()
}
KNASTA_TO_STORE_ID.update({
    "pc factory": "pcfactory",
    "mercado libre": "mercadolibre",
    "paris.cl": "paris",
    "lider.cl": "lider",
    "falabella.com": "falabella",
})

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9",
}


def parse_clp_price(val: Any) -> float:
    """
    Parsea un precio numérico o string en pesos chilenos (CLP) a float.
    
    En el mercado chileno:
    - Los precios de retail NO tienen centavos.
    - El punto '.' suele ser separador de miles ('99.990', '899.990', '1.299.990').
    - Algunas APIs retornan strings con decimales técnicos '.00' ('59990.00').
    - Algunas fuentes usan coma ',' como separador de miles ('99,990').
    """
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return round(float(val), 2)

    val_str = str(val).strip()
    if not val_str:
        return 0.0

    # Limpiar símbolos monetarios, letras y espacios
    cleaned = re.sub(r"[^\d,\.]", "", val_str)
    if not cleaned:
        return 0.0

    # Caso 1: Tiene tanto '.' como ',' (ej: '1.299.990,00' o '1,299,990.00')
    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(".") > cleaned.rfind(","):
            # '1,299,990.00'
            cleaned = cleaned.replace(",", "")
        else:
            # '1.299.990,00'
            cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return round(float(cleaned), 2)
        except Exception:
            return 0.0

    # Caso 2: Solo contiene puntos '.'
    if "." in cleaned:
        parts = cleaned.split(".")
        if len(parts) > 2:
            # '1.299.990' -> miles
            cleaned = cleaned.replace(".", "")
        else:
            # Distinguir '99.990' (miles) vs '59990.00' (decimal API)
            last_part = parts[-1]
            if len(last_part) == 3:
                cleaned = cleaned.replace(".", "")
            else:
                pass
        try:
            return round(float(cleaned), 2)
        except Exception:
            return 0.0

    # Caso 3: Solo contiene comas ','
    if "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) > 2:
            cleaned = cleaned.replace(",", "")
        else:
            last_part = parts[-1]
            if len(last_part) == 3:
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", ".")
        try:
            return round(float(cleaned), 2)
        except Exception:
            return 0.0

    # Caso 4: Solo dígitos
    try:
        return round(float(cleaned), 2)
    except Exception:
        return 0.0


def knasta_normal_price(k_item: Dict[str, Any], current_price: float) -> float:
    """
    Precio de referencia de un producto de Knasta. Knasta calcula su % de descuento contra
    'prev_price' (precio anterior registrado); 'price_tm' suele coincidir con el precio actual,
    lo que dejaba ofertas reales (ej: -48%) con 0% de descuento en el radar.
    """
    prev = parse_clp_price(k_item.get("prev_price"))
    tm = parse_clp_price(k_item.get("price_tm"))
    normal = max(prev, tm)
    return normal if normal > 0.0 else current_price


# Caché breve de páginas de Knasta: la API no filtra por tienda, así que las 4 tiendas
# que se leen vía agregador comparten las mismas páginas en cada ciclo de escaneo.
_KNASTA_CACHE_TTL_SECONDS = 120.0
_knasta_cache: Dict[str, Any] = {}
_knasta_cache_lock = threading.Lock()


def infer_category(title: str) -> str:
    """Infiere la categoría tecnológica en base al título."""
    t = title.lower()
    if any(w in t for w in ("funda", "carcasa", "case", "lámina", "vidrio templado", "cable", "adaptador", "correa")):
        return "accesorios"
    if any(w in t for w in ("macbook", "notebook", "laptop", "thinkpad", "zenbook", "ideapad")):
        return "notebooks"
    if any(w in t for w in ("iphone", "samsung galaxy", "celular", "smartphone", "xiaomi", "motorola", "redmi")):
        return "smartphones"
    if any(w in t for w in ("playstation", "ps5", "ps4", "xbox", "nintendo switch", "consola")):
        return "consolas"
    if any(w in t for w in ("smart tv", "televisor", "oled", "qled", "uhd 4k", "roku")):
        return "televisores"
    if any(w in t for w in ("audífonos", "auriculares", "airpods", "parlante", "soundbar", "barra de sonido")):
        return "audio"
    if any(w in t for w in ("tarjeta de video", "geforce", "rtx", "ryzen", "procesador", "placa madre", "ssd", "ram")):
        return "componentes"
    return "tecnologia"


class LiveRetailFeed:
    """
    Extractor de ofertas y productos reales en vivo para los 6 retailers de Chile.
    """

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout
        self.session = default_stealth_client

    def fetch_live_products_for_store(
        self,
        store_id: str,
        limit: int = 50,
        sort_strategy: Optional[str] = None,
        category_id: Optional[int] = None,
        category_ids: Optional[List[int]] = None,
    ) -> List[ProductItem]:
        """
        Obtiene productos reales con stock y links verificados para una tienda específica.
        Filtra por las categorías de SoloTodo del ciclo de rotación (category_ids) y usa
        'sort_strategy' ('price_asc' o 'discount_desc') para el orden de Knasta.
        """
        cats = list(category_ids or ([] if category_id is None else [category_id]))
        config = STORE_MAPPING.get(store_id.lower())
        if not config:
            logger.warning(f"Tienda no configurada en LiveRetailFeed: {store_id}")
            return []

        products: List[ProductItem] = []
        seen_urls: set = set()

        # 1. Extracción desde SoloTodo (Live Entities de la tienda)
        solotodo_id = config["solotodo_id"]
        try:
            # /entities/ ignora en silencio órdenes como 'offer_price_usd' o '-discount_percentage'
            # y devuelve siempre las entidades más antiguas (ej: cartuchos de tinta en Falabella).
            # '-id' sí se aplica (productos más recientes); el filtro por categoría acota el catálogo.
            cat_param = "".join(f"&categories={c}" for c in cats)
            url = (
                f"https://api.solotodo.com/entities/?is_available=1&stores={solotodo_id}"
                f"&ordering=-id{cat_param}&page_size={limit}"
            )
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                for e in results:
                    reg = e.get("active_registry")
                    if not reg or not reg.get("is_available"):
                        continue
                    ext_url = e.get("external_url")
                    if not ext_url or ext_url in seen_urls:
                        continue
                    seen_urls.add(ext_url)

                    curr_price = parse_clp_price(reg.get("offer_price"))
                    norm_price = parse_clp_price(reg.get("normal_price"))
                    if curr_price <= 0.0:
                        continue
                    if norm_price <= 0.0:
                        norm_price = curr_price

                    title = str(e.get("name", "")).strip()
                    if not title:
                        continue

                    pics = e.get("picture_urls") or []
                    img_url = pics[0] if pics else None

                    # ID derivado de la URL de la tienda para coincidir con la lectura directa
                    prod_id = canonical_product_id(store_id, ext_url) or f"{store_id}-{e.get('id')}"
                    cat = infer_category(title)

                    products.append(
                        ProductItem(
                            id=prod_id,
                            store_id=store_id,
                            store_name=config["name"],
                            title=title,
                            category=cat,
                            current_price=curr_price,
                            normal_price=norm_price,
                            url=ext_url,
                            image_url=img_url,
                            in_stock=True,
                            timestamp=datetime.now(timezone.utc),
                            source="solotodo",
                        )
                    )
        except Exception as exc:
            logger.debug(f"Error consultando SoloTodo para {store_id}: {exc}")

        # 2. Extracción desde Knasta Deals (Ofertas destacadas con descuento u orden por precio)
        try:
            knasta_name = config.get("knasta_name")
            if knasta_name:
                k_order = "price_asc" if sort_strategy == "price_asc" else "discount"
                for page_num in (1, 2, 3):
                    try:
                        k_url = f"https://knasta.cl/api/results?page={page_num}&order={k_order}"
                        for k_item in self._knasta_page(k_url):
                            ret = str(k_item.get("retail", "")).lower()
                            if ret != knasta_name:
                                continue
                            ext_url = k_item.get("url")
                            if not ext_url or ext_url in seen_urls:
                                continue
                            seen_urls.add(ext_url)

                            curr_price = parse_clp_price(k_item.get("price_value"))
                            if curr_price <= 0.0:
                                continue
                            norm_price = knasta_normal_price(k_item, curr_price)

                            title = str(k_item.get("title", "")).strip()
                            if not title:
                                continue

                            prod_id = canonical_product_id(store_id, ext_url) or f"{store_id}-k{k_item.get('product_id')}"
                            img_url = k_item.get("images")
                            cat = infer_category(title)

                            products.append(
                                ProductItem(
                                    id=prod_id,
                                    store_id=store_id,
                                    store_name=config["name"],
                                    title=title,
                                    category=cat,
                                    current_price=curr_price,
                                    normal_price=norm_price,
                                    url=ext_url,
                                    image_url=img_url,
                                    in_stock=True,
                                    timestamp=datetime.now(timezone.utc),
                                    source="knasta",
                                )
                            )
                    except Exception as exc:
                        logger.debug(f"Error consultando Knasta página {page_num} para {store_id}: {exc}")
        except Exception as exc:
            logger.debug(f"Error consultando Knasta para {store_id}: {exc}")

        return products

    def _knasta_page(self, url: str) -> List[Dict[str, Any]]:
        """Productos de una página de Knasta, con caché compartida de corta duración."""
        now = time.monotonic()
        with _knasta_cache_lock:
            cached = _knasta_cache.get(url)
            if cached and now - cached[0] < _KNASTA_CACHE_TTL_SECONDS:
                return cached[1]
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            return []
        products = resp.json().get("products", []) or []
        with _knasta_cache_lock:
            _knasta_cache[url] = (now, products)
        return products

    def search_live_products(
        self,
        query: str,
        store_ids: Optional[List[str]] = None,
        order: str = "price_asc",
        limit: int = 40,
    ) -> List[ProductItem]:
        """
        Búsqueda proactiva en vivo por término o palabra clave (ej: para Watchlist).
        Consulta Knasta y SoloTodo ordenando por 'price_asc' o 'discount' para
        identificar errores de precio en productos específicos buscados por el usuario.
        """
        clean_q = query.strip()
        if not clean_q:
            return []

        products: List[ProductItem] = []
        seen_urls: set = set()
        encoded_q = urllib.parse.quote(clean_q)

        # 1. Consulta proactiva en Knasta (agrupador multi-retailer con orden por precio)
        try:
            k_order = "price_asc" if order == "price_asc" else "discount"
            k_url = f"https://knasta.cl/api/results?q={encoded_q}&order={k_order}"
            resp = self.session.get(k_url, timeout=self.timeout)
            if resp.status_code == 200:
                raw_products = resp.json().get("products", [])
                for k_item in raw_products:
                    retail_raw = str(k_item.get("retail", "")).lower().strip()
                    target_store = KNASTA_TO_STORE_ID.get(retail_raw)
                    if not target_store:
                        continue
                    if store_ids and target_store not in store_ids:
                        continue

                    ext_url = k_item.get("url")
                    if not ext_url or ext_url in seen_urls:
                        continue
                    seen_urls.add(ext_url)

                    curr_price = parse_clp_price(k_item.get("price_value"))
                    if curr_price <= 0.0:
                        continue
                    norm_price = knasta_normal_price(k_item, curr_price)

                    title = str(k_item.get("title", "")).strip()
                    if not title:
                        continue

                    cfg = STORE_MAPPING.get(target_store, {})
                    products.append(
                        ProductItem(
                            id=canonical_product_id(target_store, ext_url) or f"{target_store}-k{k_item.get('product_id')}",
                            store_id=target_store,
                            store_name=cfg.get("name", target_store.capitalize()),
                            title=title,
                            category=infer_category(title),
                            current_price=curr_price,
                            normal_price=norm_price,
                            url=ext_url,
                            image_url=k_item.get("images"),
                            in_stock=True,
                            timestamp=datetime.now(timezone.utc),
                            source="knasta",
                        )
                    )
        except Exception as exc:
            logger.debug("Error en búsqueda proactiva Knasta para '%s': %s", clean_q, exc)

        return products[:limit]

        return products
