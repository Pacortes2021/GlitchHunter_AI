"""
Lectura Directa de Tiendas (Falabella y Ripley) para el Modo Híbrido.

Los agregadores (SoloTodo / Knasta) cubren todo el catálogo, pero sus precios llegan con
~40 minutos de retraso. Este lector consulta directamente las páginas de la tienda
(datos Next.js en __NEXT_DATA__) para:
  1. Escanear con mayor frecuencia lo prioritario (términos de la watchlist y categorías elegidas).
  2. Confirmar en tiempo real un candidato detectado vía agregador antes de alertar.

Incluye un freno automático por tienda: ante 403/429/5xx, una verificación anti-bot o una
página sin datos, la lectura directa de esa tienda se pausa con espera exponencial en vez de insistir.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.scrapers.product_ids import canonical_product_id, ripley_id_from_parent
from glitchhunter_ai.scrapers.stealth_client import default_stealth_client

logger = logging.getLogger("glitchhunter.scrapers.direct_reader")

DIRECT_STORES = ("falabella", "ripley", "sodimac", "pcfactory")

# PC Factory expone una API JSON pública (la que usa su propio sitio): búsqueda, catálogo completo
# paginado (search=*) y detalle por id. El catálogo (~3.000 productos, 48 por página) se recorre
# por partes en cada ciclo para no saturar su API: con 10 páginas por ciclo de 3 min, una vuelta
# completa toma ~20 min.
PCFACTORY_API = "https://api.pcfactory.cl/pcfactory-services-catalogo/v1/catalogo/productos"
PCFACTORY_HEADERS = {"Origin": "https://www.pcfactory.cl", "Referer": "https://www.pcfactory.cl/"}
PCFACTORY_PAGE_SIZE = 48

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
_BOT_CHALLENGE_MARKERS = ("captcha", "perimeterx", "px-captcha", "are you a robot", "access denied")

STORE_NAMES = {"falabella": "Falabella", "ripley": "Ripley", "sodimac": "Sodimac", "pcfactory": "PC Factory"}
STORE_BASE = {
    "falabella": "https://www.falabella.com",
    "ripley": "https://simple.ripley.cl",
    "sodimac": "https://www.sodimac.cl",
    "pcfactory": "https://www.pcfactory.cl",
}

# Categorías verificadas para lectura directa (clave -> ruta)
CATEGORY_PATHS: Dict[str, Dict[str, str]] = {
    "falabella": {
        "tecnologia": "/falabella-cl/category/cat20002/Tecnologia",
        "smartphones": "/falabella-cl/category/cat70008/Smartphones-y-Telefonia",
        "notebooks": "/falabella-cl/category/cat2018/Computacion",
        "consolas": "/falabella-cl/category/cat2012/Consolas-y-Videojuegos",
        "televisores": "/falabella-cl/category/cat2070/Televisores",
    },
    # Sodimac: sus rutas de categoría redirigen a la portada; la búsqueda sí funciona (y a veces
    # redirige a una página /lista/ con el formato de Falabella, que el parser también entiende).
    "sodimac": {
        "televisores": "/sodimac-cl/lista/cat7190148/Smart-TV",
        "linea_blanca": "/sodimac-cl/search?Ntt=refrigerador",
        "lavadoras": "/sodimac-cl/search?Ntt=lavadora",
        "herramientas": "/sodimac-cl/search?Ntt=taladro",
    },
    "ripley": {
        "smartphones": "/tecno/celulares",
        "notebooks": "/tecno/computacion/notebooks",
        "televisores": "/tecno/television",
        "consolas": "/tecno/mundo-gamer",
    },
}


class BlockedError(Exception):
    """La tienda respondió con un bloqueo o una página sin datos utilizables."""


class StoreBackoff:
    """Pausa con espera exponencial por tienda tras un bloqueo (10 min, 20, 40… hasta 2 h)."""

    def __init__(self, base_seconds: float = 600.0, max_seconds: float = 7200.0, clock: Callable[[], float] = time.monotonic):
        self.base = base_seconds
        self.max = max_seconds
        self.clock = clock
        self._failures: Dict[str, int] = {}
        self._paused_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_paused(self, store_id: str) -> bool:
        with self._lock:
            return self.clock() < self._paused_until.get(store_id, 0.0)

    def remaining_seconds(self, store_id: str) -> float:
        with self._lock:
            return max(0.0, self._paused_until.get(store_id, 0.0) - self.clock())

    def record_block(self, store_id: str, reason: str) -> float:
        with self._lock:
            n = self._failures.get(store_id, 0) + 1
            self._failures[store_id] = n
            wait = min(self.max, self.base * (2 ** (n - 1)))
            self._paused_until[store_id] = self.clock() + wait
        logger.warning("⛔ [Lectura directa] %s bloqueó o no entregó datos (%s). Pausa de %.0f min.", store_id, reason, wait / 60)
        return wait

    def record_success(self, store_id: str) -> None:
        with self._lock:
            self._failures.pop(store_id, None)
            self._paused_until.pop(store_id, None)


def _extract_next_data(html: str) -> Dict[str, Any]:
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        raise BlockedError("página sin __NEXT_DATA__")
    return json.loads(m.group(1))


def _clean_price(val: Any) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, list):
        val = val[0] if val else ""
    digits = re.sub(r"[^\d]", "", str(val))
    return float(digits) if digits else 0.0


def _falabella_prices(prices: List[Dict[str, Any]]) -> tuple:
    """Clasifica los precios de Falabella: (actual, normal, cmr)."""
    current = normal = 0.0
    card: Optional[float] = None
    for p in prices or []:
        ptype = str(p.get("type", "")).lower()
        val = _clean_price(p.get("price"))
        if val <= 0:
            continue
        if "normal" in ptype:
            normal = normal or val
        elif "cmr" in ptype:
            card = val
        elif current == 0.0 or val < current:
            current = val
    if current <= 0 and normal > 0:
        current = normal
    if normal <= 0:
        normal = current
    if card is not None and card >= current:
        card = None
    return current, normal, card


def _infer_category(title: str, default: str) -> str:
    t = title.lower()
    rules = (
        (("notebook", "laptop", "macbook"), "notebooks"),
        (("celular", "smartphone", "iphone", "galaxy"), "smartphones"),
        (("smart tv", "televisor", "qled", "oled"), "televisores"),
        (("consola", "playstation", "xbox", "nintendo switch"), "consolas"),
        (("tarjeta de video", "rtx", "radeon"), "componentes"),
    )
    for keys, cat in rules:
        if any(k in t for k in keys):
            return cat
    return default


def parse_falabella_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """Productos de una página de categoría o búsqueda de Falabella."""
    data = _extract_next_data(html)
    results = data.get("props", {}).get("pageProps", {}).get("results") or []
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in results:
        title = str(raw.get("displayName") or "").strip()
        url = raw.get("url") or ""
        pid = canonical_product_id("falabella", url)
        current, normal, card = _falabella_prices(raw.get("prices"))
        if not title or not pid or current <= 0:
            continue
        media = raw.get("mediaUrls") or []
        items.append(ProductItem(
            id=pid, store_id="falabella", store_name=STORE_NAMES["falabella"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=card, url=url, image_url=media[0] if media else None, in_stock=True,
            timestamp=now, source="direct",
        ))
    return items


def parse_sodimac_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """
    Productos de Sodimac en sus dos formatos:
    - páginas /lista/ (catálogo del grupo Falabella): pageProps.results, con URL y precios eventPrice/normalPrice;
    - búsqueda: pageProps.searchProps.searchData.results, precios INTERNET/NORMAL y sin URL. Su ficha
      /sodimac-cl/product/{id} redirige a la portada, así que se enlaza como búsqueda por SKU
      (/sodimac-cl/search?Ntt={sku}), que devuelve exactamente ese producto.
    """
    data = _extract_next_data(html)
    pp = data.get("props", {}).get("pageProps", {}) or {}
    results = pp.get("results") or ((pp.get("searchProps") or {}).get("searchData") or {}).get("results") or []
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in results:
        title = str(raw.get("displayName") or "").strip()
        pid_raw = str(raw.get("productId") or "").strip()
        sku = str(raw.get("skuId") or pid_raw)
        url = raw.get("url") or (f"{STORE_BASE['sodimac']}/sodimac-cl/search?Ntt={urllib.parse.quote(sku)}" if sku else "")
        pid = canonical_product_id("sodimac", url)
        current, normal, card = _falabella_prices(raw.get("prices"))
        if not title or not pid or current <= 0:
            continue
        media = raw.get("mediaUrls") or []
        items.append(ProductItem(
            id=pid, store_id="sodimac", store_name=STORE_NAMES["sodimac"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=card, url=url, image_url=media[0] if media else None, in_stock=True,
            timestamp=now, source="direct",
        ))
    return items


def parse_sodimac_product(html: str, url: str) -> Optional[ProductItem]:
    """Página de producto /articulo/ de Sodimac (mismo formato productData que Falabella)."""
    item = parse_falabella_product(html, url)
    if item is None:
        return None
    return item.model_copy(update={
        "id": canonical_product_id("sodimac", url) or item.id.replace("falabella-", "sodimac-", 1),
        "store_id": "sodimac",
        "store_name": STORE_NAMES["sodimac"],
    })


def parse_falabella_product(html: str, url: str) -> Optional[ProductItem]:
    """Producto individual (página de detalle) de Falabella."""
    data = _extract_next_data(html)
    pd = data.get("props", {}).get("pageProps", {}).get("productData") or {}
    variants = pd.get("variants") or []
    if not pd or not variants:
        return None
    v = variants[0]
    current, normal, card = _falabella_prices(v.get("prices"))
    pid = canonical_product_id("falabella", url) or f"falabella-{str(pd.get('id', '')).lower()}"
    if current <= 0:
        return None
    return ProductItem(
        id=pid, store_id="falabella", store_name=STORE_NAMES["falabella"], title=str(pd.get("name", "")).strip(),
        category=_infer_category(str(pd.get("name", "")), "tecnologia"), current_price=current,
        normal_price=normal, card_price=card, url=url, in_stock=bool(v.get("isPurchaseable", True)),
        timestamp=datetime.now(timezone.utc), source="direct",
    )


def _ripley_product_urls(html: str) -> Dict[str, str]:
    """Mapa id canónico -> URL absoluta, a partir de los enlaces de la página."""
    urls: Dict[str, str] = {}
    for href in re.findall(r'href="(/[^"?#]+)', html):
        pid = canonical_product_id("ripley", href)
        if pid and pid not in urls:
            urls[pid] = STORE_BASE["ripley"] + href
    return urls


def parse_ripley_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """Productos de una página de categoría o búsqueda de Ripley."""
    data = _extract_next_data(html)
    fp = data.get("props", {}).get("pageProps", {}).get("findabilityProps") or {}
    products = (fp.get("data") or {}).get("products") or []
    links = _ripley_product_urls(html)
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in products:
        title = str(raw.get("name") or "").strip()
        pid = ripley_id_from_parent(raw.get("parentProductID"))
        current = _clean_price(raw.get("priceNumber"))
        if not title or not pid or current <= 0:
            continue
        normal = _clean_price(raw.get("masterPriceNumber")) or current
        card = _clean_price(raw.get("ripleyPriceNumber")) or None
        if card is not None and card >= current:
            card = None
        token = pid.split("-", 1)[1]
        url = links.get(pid) or f"{STORE_BASE['ripley']}/{token}{'p' if token.isdigit() else ''}"
        image = raw.get("primaryImage")
        items.append(ProductItem(
            id=pid, store_id="ripley", store_name=STORE_NAMES["ripley"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=card, url=url, image_url=("https:" + image) if isinstance(image, str) and image.startswith("//") else image,
            in_stock=bool(raw.get("inStock", True)), timestamp=now, source="direct",
        ))
    return items


def parse_ripley_product(html: str, url: str) -> Optional[ProductItem]:
    """Producto individual (página de detalle) de Ripley."""
    data = _extract_next_data(html)
    product = (((data.get("props", {}).get("pageProps", {}).get("detailProps") or {}).get("data") or {}).get("product")) or {}
    pps = product.get("parentpricestock") or {}
    price = pps.get("price") or {}
    current = _clean_price((price.get("sale") or {}).get("valueNumber"))
    normal = _clean_price((price.get("master") or {}).get("valueNumber")) or current
    card = _clean_price((price.get("ripley") or {}).get("valueNumber")) or None
    if not product or current <= 0:
        return None
    if card is not None and card >= current:
        card = None
    pid = canonical_product_id("ripley", url) or ripley_id_from_parent(product.get("parentProductID"))
    title = str(product.get("name", "")).strip()
    return ProductItem(
        id=pid, store_id="ripley", store_name=STORE_NAMES["ripley"], title=title,
        category=_infer_category(title, "tecnologia"), current_price=current, normal_price=normal,
        card_price=card, url=url, in_stock=bool(pps.get("stock", True)),
        timestamp=datetime.now(timezone.utc), source="direct",
    )


LISTING_PARSERS = {"falabella": parse_falabella_listing, "ripley": parse_ripley_listing, "sodimac": parse_sodimac_listing}
PRODUCT_PARSERS = {"falabella": parse_falabella_product, "ripley": parse_ripley_product, "sodimac": parse_sodimac_product}


class DirectStoreReader:
    """Consulta directa de Falabella y Ripley con freno automático ante bloqueos."""

    def __init__(self, client: Any = None, backoff: Optional[StoreBackoff] = None, timeout: float = 12.0):
        self.client = client or default_stealth_client
        self.backoff = backoff or StoreBackoff()
        self.timeout = timeout
        self._catalog_cursor: Dict[str, int] = {}
        self._catalog_pages: Dict[str, int] = {}

    # --- PC Factory (API JSON) -------------------------------------------------
    def _pcfactory_query(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Consulta la API de PC Factory con el mismo freno automático que las páginas HTML."""
        if self.backoff.is_paused("pcfactory"):
            return None
        try:
            resp = self.client.get(PCFACTORY_API, params=params, headers=PCFACTORY_HEADERS, timeout=self.timeout)
        except Exception as exc:
            logger.debug("Error de red en la API de PC Factory: %s", exc)
            return None
        status = getattr(resp, "status_code", 0)
        if status in (403, 429) or status >= 500:
            self.backoff.record_block("pcfactory", f"HTTP {status}")
            return None
        if status != 200:
            return None
        try:
            content = resp.json().get("content") or {}
        except Exception:
            self.backoff.record_block("pcfactory", "respuesta no es JSON")
            return None
        self.backoff.record_success("pcfactory")
        return content

    def _pcfactory_items(self, content: Optional[Dict[str, Any]]) -> List[ProductItem]:
        from glitchhunter_ai.scrapers.pcfactory import PCFactoryScraper

        if not content:
            return []
        return PCFactoryScraper().parse_pcfactory_json(content.get("items") or [])

    def fetch_catalog_slice(self, store_id: str, pages: int = 10) -> List[ProductItem]:
        """
        Siguiente tramo del catálogo completo de la tienda (rotación por páginas).
        Hoy solo PC Factory lo permite: su API lista todo el catálogo con search=*.
        """
        if store_id != "pcfactory":
            return []
        items: List[ProductItem] = []
        start = self._catalog_cursor.get(store_id, 0)
        total = self._catalog_pages.get(store_id)
        page = start
        for _ in range(max(1, pages)):
            content = self._pcfactory_query({"page": page, "size": PCFACTORY_PAGE_SIZE, "search": "*"})
            if content is None:
                break
            total = (content.get("pageable") or {}).get("totalPages") or total
            items.extend(self._pcfactory_items(content))
            page += 1
            if total and page >= total:
                page = 0
                break
        if total:
            self._catalog_pages[store_id] = total
        self._catalog_cursor[store_id] = page
        return items

    def _get_html(self, store_id: str, url: str) -> Optional[str]:
        """Descarga la página; ante bloqueo activa la pausa de la tienda y retorna None."""
        if self.backoff.is_paused(store_id):
            logger.debug("Lectura directa de %s en pausa (%.0f s restantes).", store_id, self.backoff.remaining_seconds(store_id))
            return None
        try:
            resp = self.client.get(url, timeout=self.timeout)
        except Exception as exc:
            logger.debug("Error de red en lectura directa de %s: %s", store_id, exc)
            return None
        status = getattr(resp, "status_code", 0)
        if status in (403, 429) or status >= 500:
            self.backoff.record_block(store_id, f"HTTP {status}")
            return None
        if status != 200:
            return None
        html = resp.text or ""
        if "__NEXT_DATA__" not in html:
            lower = html[:20000].lower()
            reason = "verificación anti-bot" if any(m in lower for m in _BOT_CHALLENGE_MARKERS) else "página sin datos"
            self.backoff.record_block(store_id, reason)
            return None
        return html

    def _listing(self, store_id: str, url: str, category_hint: str) -> List[ProductItem]:
        html = self._get_html(store_id, url)
        if html is None:
            return []
        try:
            items = LISTING_PARSERS[store_id](html, category_hint)
        except (BlockedError, ValueError, KeyError) as exc:
            self.backoff.record_block(store_id, f"formato inesperado: {exc}")
            return []
        self.backoff.record_success(store_id)
        return items

    def fetch_category(self, store_id: str, category_key: str) -> List[ProductItem]:
        path = CATEGORY_PATHS.get(store_id, {}).get(category_key)
        if not path:
            return []
        return self._listing(store_id, STORE_BASE[store_id] + path, category_key)

    def search(self, store_id: str, keyword: str) -> List[ProductItem]:
        q = urllib.parse.quote(keyword.strip())
        if store_id == "falabella":
            url = f"{STORE_BASE['falabella']}/falabella-cl/search?Ntt={q}"
        elif store_id == "ripley":
            url = f"{STORE_BASE['ripley']}/search/{q}"
        elif store_id == "sodimac":
            url = f"{STORE_BASE['sodimac']}/sodimac-cl/search?Ntt={q}"
        elif store_id == "pcfactory":
            return self._pcfactory_items(
                self._pcfactory_query({"page": 0, "size": PCFACTORY_PAGE_SIZE, "search": keyword.strip()})
            )
        else:
            return []
        return self._listing(store_id, url, "tecnologia")

    def fetch_product(self, store_id: str, url: str) -> Optional[ProductItem]:
        """Precio en tiempo real de un producto. None si la tienda no responde o está en pausa."""
        if store_id == "pcfactory":
            pid = (canonical_product_id("pcfactory", url) or "").split("-", 1)[-1]
            if not pid.isdigit():
                return None
            content = self._pcfactory_query({"page": 0, "size": 5, "search": pid})
            matches = [i for i in self._pcfactory_items(content) if i.id == f"pcfactory-{pid}"]
            return matches[0] if matches else None
        if store_id not in PRODUCT_PARSERS:
            return None
        html = self._get_html(store_id, url)
        if html is None:
            return None
        try:
            item = PRODUCT_PARSERS[store_id](html, url)
        except (BlockedError, ValueError, KeyError) as exc:
            self.backoff.record_block(store_id, f"formato inesperado: {exc}")
            return None
        self.backoff.record_success(store_id)
        return item


default_direct_reader = DirectStoreReader()
