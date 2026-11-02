"""
Módulo Sniper de Compra Rápida (Direct Add-to-Cart / Fast Checkout).
Genera enlaces de compra directa en 1 clic para reducir el tiempo de checkout
y ganar las ofertas y glitches antes de que se agote el stock.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Optional


def generate_direct_add_to_cart_url(store_id: str, product_url: str, product_id: Optional[str] = None) -> Optional[str]:
    """
    Genera la URL directa para agregar al carrito o saltar directo al checkout
    en base a la tienda y los identificadores de producto/SKU.
    """
    if not product_url:
        return None

    sid = (store_id or "").lower()
    url = product_url.strip()

    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        domain = parsed.netloc

        # 1. Falabella Chile
        if "falabella" in sid or "falabella.com" in domain:
            # Buscar SKU en URL: /product/16912389/... o /16912389
            match = re.search(r"/(\d{6,12})", path)
            sku = match.group(1) if match else product_id
            if sku:
                clean_sku = re.sub(r"[^\d]", "", str(sku))
                if clean_sku:
                    return f"https://www.falabella.com/falabella-cl/checkout?skuId={clean_sku}"
            return url

        # 2. Ripley Chile (Plataforma VTEX / Web propia)
        if "ripley" in sid or "ripley.cl" in domain:
            # En Ripley las URLs terminan en -2000390111p
            match = re.search(r"-(\d{6,12})p?", path)
            sku = match.group(1) if match else product_id
            if sku:
                clean_sku = re.sub(r"[^\d]", "", str(sku))
                if clean_sku:
                    return f"https://simple.ripley.cl/checkout/cart/add?sku={clean_sku}&qty=1&seller=1"
            return url

        # 3. Líder Chile (Walmart)
        if "lider" in sid or "lider.cl" in domain:
            match = re.search(r"/sku/(\d+)", path)
            sku = match.group(1) if match else product_id
            if sku:
                clean_sku = re.sub(r"[^\d]", "", str(sku))
                if clean_sku:
                    return f"https://www.lider.cl/catalogo/cart/add?sku={clean_sku}&quantity=1"
            return url

        # 4. PC Factory
        if "pcfactory" in sid or "pcfactory.cl" in domain:
            match = re.search(r"/producto/(\d+)", path) or re.search(r"-(\d+)$", path)
            sku = match.group(1) if match else product_id
            if sku:
                clean_sku = re.sub(r"[^\d]", "", str(sku))
                if clean_sku:
                    return f"https://www.pcfactory.cl/carro/agregar?id={clean_sku}"
            return url

        # 5. Mercado Libre Chile
        if "mercadolibre" in sid or "mercadolibre.cl" in domain:
            match = re.search(r"(MLC-?\d+)", path, re.IGNORECASE)
            item_id = match.group(1).replace("-", "") if match else None
            if item_id:
                return f"https://articulo.mercadolibre.cl/checkout/buyer/comprar-ahora?itemId={item_id}"
            return url

        # 6. Paris (Cencosud)
        if "paris" in sid or "paris.cl" in domain:
            match = re.search(r"-(\d{7,10})\.html", path)
            sku = match.group(1) if match else None
            if sku:
                return f"https://www.paris.cl/cart-add?pid={sku}&quantity=1"
            return url

        # 7. Tiendas genéricas Shopify (detectadas por /products/ o tiendas como SP Digital / Digitek)
        if "/products/" in path:
            # URL de compra directa en Shopify
            return f"{parsed.scheme}://{domain}/cart/add?url={urllib.parse.quote(url)}"

    except Exception:
        pass

    # Si no se pudo generar enlace directo especial, el enlace oficial del producto es el sniper fallback
    return url
