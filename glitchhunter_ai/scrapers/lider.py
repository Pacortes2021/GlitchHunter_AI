"""
Módulo Scraper para Líder.cl (Walmart Chile) - GlitchHunter AI.

Extrae catálogos de productos y ofertas de Líder.cl, con soporte para
precios normales, precios oferta y precios exclusivos con Tarjeta Líder BCI.
Incluye capacidad de consulta HTTP estructurada y simulación de alta fidelidad
con patrones típicos de errores de fijación de precios (pérdida de ceros) y
casos trampa de accesorios.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.parse

# Asegurar que el directorio raíz del proyecto esté en el path para importaciones directas
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from glitchhunter_ai.core.models import ProductItem

logger = logging.getLogger("glitchhunter.scrapers.lider")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [LiderScraper] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class LiderScraper:
    """
    Scraper y extractor de ofertas para Líder.cl (Walmart Chile).
    Monitorea categorías clave: tecnología, electrohogar, movilidad, supermercado y terraza.
    """

    store_id: str = "lider"
    store_name: str = "Líder Chile"
    base_url: str = "https://www.lider.cl"
    search_api_url: str = "https://www.lider.cl/catalogo/api/products"

    # Headers estándar emulando un navegador moderno en Chile
    DEFAULT_HEADERS: Dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.lider.cl/catalogo/",
        "Origin": "https://www.lider.cl",
    }

    def __init__(self, timeout: float = 6.0) -> None:
        """
        Inicializa el scraper con timeout y parámetros de conexión.
        """
        self.timeout = timeout

    def fetch_catalog(self, use_live: bool = False) -> List[ProductItem]:
        """
        Punto de entrada principal para obtener el catálogo de productos.
        
        Args:
            use_live: Si es True, intenta consultar endpoints en vivo de Líder.cl.
                      Si falla o se bloquea por Cloudflare/Akamai, hace fallback seguro al catálogo simulado.
                      Si es False, retorna directamente el catálogo de simulación de alta fidelidad.
        
        Returns:
            Lista de objetos ProductItem listos para el motor de detección de glitches.
        """
        if use_live:
            logger.info("Modo LIVE activado. Intentando consultar endpoints de Líder.cl...")
            live_items = self.fetch_live_catalog()
            if not live_items:
                try:
                    from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
                    live_items = LiveRetailFeed().fetch_live_products_for_store("lider")
                except Exception as exc:
                    logger.debug(f"Error consultando LiveRetailFeed para Líder: {exc}")

            if live_items:
                logger.info("Éxito en vivo: Se obtuvieron %d productos reales de Líder.cl", len(live_items))
                return live_items
            logger.warning(
                "Fallo en consulta en vivo o bloqueo antibot en Líder.cl. "
                "Modo LIVE activado: retornando lista vacía (sin fallback a simulaciones)."
            )
            return []

        logger.debug("Modo DEMO: Cargando catálogo simulado de alta fidelidad para Líder.cl...")
        return self.get_simulated_catalog()

    def fetch_live_catalog(self, query: str = "ofertas") -> List[ProductItem]:
        """
        Realiza consulta HTTP estructurada a la API de búsqueda/catálogo de Líder.cl.
        """
        if not HAS_HTTPX:
            logger.warning("Librería 'httpx' no disponible para consulta live. Instale httpx.")
            return []

        products: List[ProductItem] = []
        params = {
            "query": query,
            "page": 1,
            "limit": 24
        }

        try:
            with httpx.Client(headers=self.DEFAULT_HEADERS, timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(self.search_api_url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    products = self.parse_lider_json(data)
                else:
                    logger.warning(
                        "Respuesta HTTP no exitosa de Líder (%d): %s",
                        response.status_code,
                        response.text[:150]
                    )
        except Exception as exc:
            logger.warning("Error durante la petición HTTP a Líder.cl: %s", str(exc))

        return products

    def parse_lider_json(self, data: Dict[str, Any]) -> List[ProductItem]:
        """
        Parsea la respuesta JSON estructurada de la API de Líder / Walmart.
        """
        items: List[ProductItem] = []
        raw_products = data.get("products") or data.get("items") or []

        for item in raw_products:
            try:
                sku = str(item.get("sku") or item.get("id") or item.get("product_id", ""))
                title = item.get("displayName") or item.get("title") or item.get("name", "Producto Líder")
                category = item.get("category") or item.get("family", "general")
                url_slug = item.get("url") or f"/catalogo/product/sku/{sku}"
                full_url = url_slug if url_slug.startswith("http") else urllib.parse.urljoin(self.base_url, url_slug)
                image_url = item.get("image") or item.get("imageUrl") or None
                in_stock = bool(item.get("inStock", True))

                # Manejo de precios
                price_info = item.get("price") or {}
                normal_p = float(price_info.get("referencePrice") or price_info.get("listPrice") or 0.0)
                current_p = float(price_info.get("salePrice") or price_info.get("currentPrice") or normal_p)
                card_p_raw = price_info.get("liderCardPrice") or price_info.get("cardPrice")
                card_p = float(card_p_raw) if card_p_raw is not None else None

                if normal_p <= 0.0 and current_p > 0.0:
                    normal_p = current_p

                if current_p > 0.0:
                    items.append(
                        ProductItem(
                            id=f"lider_{sku}" if sku else f"lider_{hash(title)}",
                            store_id=self.store_id,
                            store_name=self.store_name,
                            title=title,
                            category=category,
                            current_price=current_p,
                            normal_price=normal_p,
                            card_price=card_p,
                            url=full_url,
                            image_url=image_url,
                            in_stock=in_stock,
                            timestamp=datetime.now(timezone.utc)
                        )
                    )
            except Exception as e:
                logger.debug("Error procesando ítem individual de Líder: %s", e)

        return items

    def get_simulated_catalog(self) -> List[ProductItem]:
        """
        Genera un catálogo de alta fidelidad representativo de Líder.cl (Walmart Chile).
        
        Incluye:
        1. Glitches críticos con pérdida de ceros (~10x) en productos de alto valor.
        2. Productos insignia de Líder (Smart TV LG 55", Freidora Philips, Scooter Xiaomi,
           Pack Corona, Parrilla Weber).
        3. Precios de oferta especial con Tarjeta Líder BCI.
        4. Casos trampa de accesorios (para verificar que el filtro de IA no alerte erróneamente).
        5. Casos de precios inflados (Cyber Bait) y productos sin stock.
        """
        now = datetime.now(timezone.utc)

        simulated_data = [
            # 🚨 GLITCH CRÍTICO 1: Smart TV LG 55" 4K - Error de falta de un cero ($429.990 -> $42.990)
            ProductItem(
                id="LID-SKU-1029381",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Smart TV LG 55\" 4K UHD ThinQ AI 55UR7800PSB",
                category="tecnologia / televisores",
                current_price=42990.0,
                normal_price=429990.0,
                card_price=39990.0,  # Tarjeta Líder BCI
                url="https://www.lider.cl/catalogo/product/sku/1029381/lg-smart-tv-55-4k-uhd-55ur7800psb",
                image_url="https://images.lider.cl/products/1029381_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🚨 GLITCH CRÍTICO 2: Scooter Eléctrico Xiaomi - Pérdida de un cero ($399.990 -> $39.990)
            ProductItem(
                id="LID-SKU-2091823",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Scooter Eléctrico Xiaomi Electric Scooter 4 Go 20km/h",
                category="movilidad urbana",
                current_price=39990.0,
                normal_price=399990.0,
                card_price=36990.0,  # Tarjeta Líder BCI
                url="https://www.lider.cl/catalogo/product/sku/2091823/xiaomi-electric-scooter-4-go",
                image_url="https://images.lider.cl/products/2091823_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🚨 GLITCH CRÍTICO 3: Parrilla a Gas Weber - Pérdida de un cero ($649.990 -> $64.990)
            ProductItem(
                id="LID-SKU-3182746",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Parrilla a Gas Weber Spirit II E-310 3 Quemadores Negra",
                category="terraza y aire libre",
                current_price=64990.0,
                normal_price=649990.0,
                card_price=59990.0,  # Tarjeta Líder BCI
                url="https://www.lider.cl/catalogo/product/sku/3182746/weber-parrilla-a-gas-spirit-ii-e-310",
                image_url="https://images.lider.cl/products/3182746_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ⚠️ CASO TRAMPA 1 (Accesorio de TV): Soporte de pared a $14.990 (No es una TV)
            ProductItem(
                id="LID-SKU-4918273",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Soporte de Pared Fijo para Smart TV 32\" a 55\" Pulgadas Universal",
                category="accesorios tecnologia",
                current_price=14990.0,
                normal_price=22990.0,
                card_price=12990.0,
                url="https://www.lider.cl/catalogo/product/sku/4918273/soporte-pared-tv-32-55-pulgadas",
                image_url="https://images.lider.cl/products/4918273_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ⚠️ CASO TRAMPA 2 (Accesorio de Movilidad): Casco y funda para scooter a $15.990
            ProductItem(
                id="LID-SKU-5819283",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Casco Urbano y Funda Protectora de Manillar para Scooter Eléctrico Xiaomi",
                category="accesorios movilidad",
                current_price=15990.0,
                normal_price=27990.0,
                card_price=13990.0,
                url="https://www.lider.cl/catalogo/product/sku/5819283/casco-funda-scooter-xiaomi",
                image_url="https://images.lider.cl/products/5819283_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ✅ OFERTA LEGÍTIMA 1: Freidora de Aire Philips XL 6.2L (Descuento normal de retail ~35%)
            ProductItem(
                id="LID-SKU-6192847",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Freidora de Aire Philips Essential Airfryer XL 6.2L 2000W HD9270",
                category="electrohogar / cocina",
                current_price=89990.0,
                normal_price=139990.0,
                card_price=79990.0,  # Tarjeta Líder BCI
                url="https://www.lider.cl/catalogo/product/sku/6192847/philips-freidora-aire-essential-xl",
                image_url="https://images.lider.cl/products/6192847_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ✅ OFERTA LEGÍTIMA 2: Supermercado Pack Cervezas Corona 24 unidades
            ProductItem(
                id="LID-SKU-7281934",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Pack Cervezas Corona Extra Botella 24 x 355cc",
                category="supermercado / botilleria",
                current_price=19990.0,
                normal_price=28990.0,
                card_price=17990.0,  # Tarjeta Líder BCI
                url="https://www.lider.cl/supermercado/product/sku/7281934/pack-cervezas-corona-extra-24x355cc",
                image_url="https://images.lider.cl/products/7281934_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ✅ OFERTA LEGÍTIMA 3: Notebook Gamer ASUS TUF
            ProductItem(
                id="LID-SKU-8392019",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Notebook Gamer ASUS TUF Gaming F15 15.6\" Intel Core i5 16GB RAM 512GB SSD RTX 3050",
                category="computacion / notebooks",
                current_price=649990.0,
                normal_price=799990.0,
                card_price=599990.0,  # Tarjeta Líder BCI
                url="https://www.lider.cl/catalogo/product/sku/8392019/asus-tuf-gaming-f15-i5-rtx3050",
                image_url="https://images.lider.cl/products/8392019_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🔍 PRECIO INFLADO (Cyber Bait): Barra de Sonido Samsung inflada a $189.990
            ProductItem(
                id="LID-SKU-9401928",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Barra de Sonido Samsung HW-C400 2.0 Canales Bluetooth",
                category="audio",
                current_price=69990.0,
                normal_price=189990.0,  # Precio normal inflado artificialmente
                card_price=59990.0,
                url="https://www.lider.cl/catalogo/product/sku/9401928/samsung-barra-sonido-hw-c400",
                image_url="https://images.lider.cl/products/9401928_large.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🚫 AGOTADO: Consola PlayStation 5 Slim
            ProductItem(
                id="LID-SKU-1092837",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Consola PlayStation 5 Slim Edición Digital con 2 Juegos",
                category="videojuegos / consolas",
                current_price=499990.0,
                normal_price=569990.0,
                card_price=479990.0,
                url="https://www.lider.cl/catalogo/product/sku/1092837/playstation-5-slim-digital",
                image_url="https://images.lider.cl/products/1092837_large.jpg",
                in_stock=False,  # Sin stock
                timestamp=now,
            ),
        ]

        return simulated_data


if __name__ == "__main__":
    print("=" * 70)
    print("🛒 GlitchHunter AI - Prueba de Scraper: Líder.cl (Walmart Chile)")
    print("=" * 70)

    scraper = LiderScraper()
    print(f"Tienda configurada: {scraper.store_name} ({scraper.store_id})")
    
    # Obtención de catálogo
    catalog = scraper.fetch_catalog(use_live=False)
    print(f"\n📦 Total de productos obtenidos: {len(catalog)}\n")

    for idx, item in enumerate(catalog, 1):
        best_p = item.best_price
        disc = item.discount_percentage
        savings = item.absolute_savings_clp
        card_str = f" | Tarjeta Líder BCI: ${item.card_price:,.0f}" if item.card_price else ""
        stock_str = "🟢 EN STOCK" if item.in_stock else "🔴 AGOTADO"

        print(f"[{idx:02d}] {item.title}")
        print(f"     SKU: {item.id} | Cat: {item.category} | {stock_str}")
        print(f"     Normal: ${item.normal_price:,.0f} | Oferta: ${item.current_price:,.0f}{card_str}")
        print(f"     Mejor Precio: ${best_p:,.0f} -> Descuento: {disc:.1f}% | Ahorro: ${savings:,.0f} CLP")
        print(f"     URL: {item.url}")
        print("-" * 70)
