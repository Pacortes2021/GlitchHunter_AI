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
# ABC (ex La Polar, Salesforce Commerce Cloud): /{slug}/{pid}.html
_ABC_RE = re.compile(r"/(\d{6,})\.html$")
_JUMBO_RE = re.compile(r"^/([a-z0-9_-]+)/p$")   # URLs /{slug}/p de Jumbo y tiendas VTEX (Tramontina usa "_")
_SLUG_P_STORES = {"jumbo", "santaisabel", "sony", "motorola", "electrolux", "fensa", "oster", "casaroyal", "nike", "levis",
                  "reebok", "mimbral", "audiomusica", "dimeiggs", "drsimi", "mademsa", "dimarsa", "marsella",
                  "construplaza", "tiendasalfa", "tramontina", "dimarine", "lavinoteca", "maxservice", "ventus",
                  "karcher", "esika", "lbel", "clubsoftys", "coleman", "marleycoffee", "bbqgrill", "blanik",
                  "cotidian", "colloky", "ficcus", "zapatos", "hm", "dolly", "ferouch", "calvinklein", "theline",
                  "tommy", "treck", "velez", "privilege", "newman", "swarovski", "womensecret", "totto",
                  "oneill", "americaneagle", "asics", "trial", "umbrale", "mountainhardwear", "ilgioco"}
_SHOPIFY_RE = re.compile(r"^/products/([a-z0-9-]+)$")   # URLs /products/{handle} de tiendas Shopify
# fichas Magento: /{slug} o /{slug}.html, a veces bajo su categoría (/ofertas/{slug} en Novasalud)
# URL interna de Magento (/catalog/product/view/id/472043/s/…/category/4742/): el producto es el número tras /id/
_MAGENTO_VIEW_RE = re.compile(r"/catalog/product/view/id/(\d+)(?:/|$)")
_MAGENTO_RE = re.compile(r"^/(?:[a-z0-9-]+/)*([a-z0-9-]+?)(?:\.html)?$")
_MAGENTO_STORES = {"thenorthface", "converse", "lacoste", "marmot", "sparta", "dbs", "antartica", "weplay", "sdn",
                   "novasalud", "ebest", "bathandbodyworks", "schilling", "flex", "headchile", "epson", "umbro",
                   "kotting", "roly", "surprice", "ash", "bananarepublic", "brooksbrothers", "calubags",
                   "dcshoes", "globe", "guess", "jayson", "kannu", "kipling", "magriffe", "mammut", "mauiandsons",
                   "mcgregor", "potros", "rapsodia", "ripcurl", "rusty", "stoked", "gap", "ugg",
                   "victoriassecret", "volcom", "wa2", "farmaciamapuche", "rosen", "cannonhome"}
_SHOPIFY_STORES = {"columbia", "caterpillar", "patagonia", "underarmour", "crocs", "vans", "doite", "havaianas",
                   "oxford", "aufbau", "lippi", "kitchencenter", "sharkninja", "dermotienda", "herramientastotal",
                   "delonghi", "santarita", "petplanet", "toyng", "juanvaldez", "promusic"}
_HP_RE = re.compile(r"/shop/[a-z0-9-]+-([a-z0-9]{5,10})\.html$")   # el SKU va al final del slug
_WINPY_RE = re.compile(r"^/venta/([a-z0-9-]+)$")
_SPDIGITAL_RE = re.compile(r"^/([a-z0-9-]+)$")   # fichas de SP Digital: /{slug}/
# Paris: /{slug}-{CLAVE}.html (clave commercetools, ej: MKDFYM0YKP; en URLs antiguas, numérica)
_PARIS_RE = re.compile(r"-([A-Za-z0-9]+)\.html$")
# Ripley: /{slug}-{partNumber}p  o  /{slug}-mpm{n} (marketplace)
_RIPLEY_RE = re.compile(r"-(\d{8,16})p$|-(mp[a-z]?\d{6,})$", re.IGNORECASE)
# Sodimac: /sodimac-cl/articulo/{id}/… (catálogo del grupo Falabella) o /sodimac-cl/product/{id}/… (búsqueda)
_SODIMAC_RE = re.compile(r"/sodimac-cl/(?:articulo|product)/([A-Za-z0-9]+)")
# PC Factory: /producto/{id}-{slug}
_PCFACTORY_RE = re.compile(r"/producto/(\d+)")
# Mercado Libre: MLC123456789 o MLC-123456789 (en la ruta o en parámetros como wid=)
_MERCADOLIBRE_RE = re.compile(r"MLC-?(\d{6,})", re.IGNORECASE)


def canonical_product_id(store_id: str, url: Optional[str]) -> Optional[str]:
    """
    Retorna '{store_id}-{id_tienda}' si la URL permite identificar el producto, o None.
    Cubre Falabella, Ripley, Paris, ABC, Jumbo, HP, Winpy, Sodimac, PC Factory, Mercado Libre y las tiendas VTEX y Shopify.
    """
    if not url:
        return None
    store = (store_id or "").lower()
    path = urllib.parse.urlparse(str(url)).path.rstrip("/")

    if store == "abc":
        m = _ABC_RE.search(path)
        return f"abc-{m.group(1)}" if m else None

    if store in _MAGENTO_STORES:
        view = _MAGENTO_VIEW_RE.search(path.lower())
        if view:
            return f"{store}-{view.group(1)}"
        m = _MAGENTO_RE.search(path.lower())
        return f"{store}-{m.group(1)}" if m else None

    if store in _SHOPIFY_STORES:
        m = _SHOPIFY_RE.search(path.lower())
        return f"{store}-{m.group(1)}" if m else None

    if store == "hp":
        m = _HP_RE.search(path.lower())
        return f"hp-{m.group(1)}" if m else None

    if store == "winpy":
        m = _WINPY_RE.search(path.lower())
        return f"winpy-{m.group(1)}" if m else None

    if store == "spdigital":
        m = _SPDIGITAL_RE.search(path.lower())
        return f"spdigital-{m.group(1)}" if m and not path.startswith("/categories") else None

    if store in _SLUG_P_STORES:
        m = _JUMBO_RE.search(path.lower())
        return f"{store}-{m.group(1)}" if m else None

    if store == "paris":
        m = _PARIS_RE.search(path)
        return f"paris-{m.group(1).lower()}" if m else None

    if store == "falabella":
        m = _FALABELLA_RE.search(path)
        return f"falabella-{m.group(1).lower()}" if m else None

    if store == "ripley":
        m = _RIPLEY_RE.search(path)
        if m:
            return f"ripley-{(m.group(1) or m.group(2)).lower()}"
        return None

    if store == "sodimac":
        m = _SODIMAC_RE.search(path)
        if m:
            return f"sodimac-{m.group(1).lower()}"
        # Productos de la búsqueda: su ficha /product/{id} redirige a la portada, así que se enlazan
        # como búsqueda por SKU (?Ntt={sku}), que devuelve exactamente ese producto
        if path.endswith("/search"):
            sku = urllib.parse.parse_qs(urllib.parse.urlparse(str(url)).query).get("Ntt", [""])[0].strip()
            return f"sodimac-{sku.lower()}" if sku else None
        return None

    if store == "pcfactory":
        m = _PCFACTORY_RE.search(path)
        return f"pcfactory-{m.group(1)}" if m else None

    if store == "mercadolibre":
        # Los enlaces de publicidad (click1.mercadolibre.cl) llevan el ID real codificado en la query
        m = _MERCADOLIBRE_RE.search(urllib.parse.unquote(str(url)))
        return f"mercadolibre-mlc{m.group(1)}" if m else None

    return None


def ripley_id_from_parent(parent_product_id: Optional[str]) -> Optional[str]:
    """'2000406555881P' -> 'ripley-2000406555881'; 'MPM10003498644' -> 'ripley-mpm10003498644'."""
    if not parent_product_id:
        return None
    pid = str(parent_product_id).strip().lower()
    if pid.isalnum() and pid[:-1].isdigit() and pid.endswith("p"):
        pid = pid[:-1]
    return f"ripley-{pid}" if pid else None
