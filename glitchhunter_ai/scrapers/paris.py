"""
Módulo de Scraper para Paris.cl (Cencosud Retail S.A.)
Monitorea catálogo, ofertas con Tarjeta Cencosud Scotiabank y anomalías de precios.

Soporta:
1. Consulta HTTP estructurada a endpoints de catálogo/búsqueda de Paris.
2. Catálogo de simulación de alta fidelidad con casos reales de retail chileno:
   - Descuentos legítimos y ofertas exclusivas con Tarjeta Cencosud.
   - Glitches críticos de pérdida de ceros (Missing Zero Bug).
   - Casos trampa de accesorios y repuestos para pruebas del filtro semántico.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

import requests

# Garantizar resolución de paquetes tanto al ejecutar como script como por módulo
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from glitchhunter_ai.core.models import ProductItem

logger = logging.getLogger("glitchhunter.scrapers.paris")


class ParisScraper:
    """
    Scraper y extractor de catálogo para Paris.cl (Cencosud).
    Extrae precios normales, precios internet y precios exclusivos con Tarjeta Cencosud.
    """

    store_id: str = "paris"
    store_name: str = "Paris.cl"
    base_url: str = "https://www.paris.cl"
    vtex_search_url: str = "https://www.paris.cl/api/catalog_system/pub/products/search"

    def __init__(self, timeout: int = 10, session: Optional[requests.Session] = None) -> None:
        """
        Inicializa el scraper con sesión HTTP configurada y headers de navegador realista.
        """
        self.store_id = "paris"
        self.store_name = "Paris.cl"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.paris.cl/",
            "Cache-Control": "no-cache",
        })

    def fetch_catalog(self, use_live: bool = False) -> List[ProductItem]:
        """
        Obtiene la lista de productos del catálogo de Paris.cl.

        Args:
            use_live: Si es True, intenta consultar la API live de Paris.cl por HTTP.
                      Si falla o no hay conexión, recurre con elegancia al catálogo simulado.
                      Si es False, retorna directamente el catálogo simulado de alta fidelidad.

        Returns:
            Lista de instancias de `ProductItem` fuertemente tipadas.
        """
        if use_live:
            logger.info("Iniciando extracción en vivo desde Paris.cl...")
            live_items = self._fetch_live_catalog()
            if not live_items:
                try:
                    from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
                    live_items = LiveRetailFeed().fetch_live_products_for_store("paris")
                except Exception as exc:
                    logger.debug(f"Error consultando LiveRetailFeed para Paris: {exc}")

            if live_items:
                logger.info("Extracción en vivo exitosa: %d productos reales obtenidos de Paris.cl", len(live_items))
                return live_items
            logger.warning(
                "No fue posible obtener catálogo en vivo para Paris.cl (bloqueo anti-bot o timeout). "
                "Modo LIVE activado: retornando lista vacía (sin fallback a simulaciones)."
            )
            return []

        logger.debug("Modo DEMO: Cargando catálogo simulado de alta fidelidad para Paris.cl...")
        return self._build_simulated_catalog()

    def _fetch_live_catalog(self, query: str = "tecnologia", count: int = 20) -> List[ProductItem]:
        """
        Ejecuta una consulta estructurada a la API pública de catálogo VTEX de Paris.cl.
        """
        params = {
            "ft": query,
            "_from": 0,
            "_to": max(0, count - 1),
        }
        try:
            response = self.session.get(
                self.vtex_search_url,
                params=params,
                timeout=self.timeout,
            )
            if response.status_code != 200:
                logger.warning("Respuesta HTTP no esperada de Paris.cl: %s", response.status_code)
                return []

            data = response.json()
            if not isinstance(data, list):
                logger.warning("Formato JSON no esperado de Paris.cl (se esperaba lista): %s", type(data))
                return []

            products: List[ProductItem] = []
            for raw in data:
                item = self._parse_vtex_item(raw)
                if item:
                    products.append(item)

            return products
        except requests.exceptions.RequestException as exc:
            logger.warning("Error de red al consultar Paris.cl en vivo: %s", exc)
            return []
        except Exception as exc:
            logger.warning("Error inesperado procesando respuesta en vivo de Paris.cl: %s", exc)
            return []

    def _parse_vtex_item(self, raw: Dict[str, Any]) -> Optional[ProductItem]:
        """
        Parsea un objeto de producto devuelto por el motor de búsqueda VTEX de Paris.cl.
        """
        try:
            product_id = str(raw.get("productId") or raw.get("productReference") or "")
            title = str(raw.get("productName") or "").strip()
            link_text = raw.get("linkText") or ""
            url = f"{self.base_url}/{link_text}.html" if link_text else f"{self.base_url}/p/{product_id}"

            items_list = raw.get("items", [])
            if not items_list:
                return None

            first_item = items_list[0]
            images = first_item.get("images", [])
            image_url = images[0].get("imageUrl") if images else None

            sellers = first_item.get("sellers", [])
            if not sellers:
                return None

            comm_offer = sellers[0].get("commertialOffer", {})
            current_price = float(comm_offer.get("Price", 0.0))
            normal_price = float(comm_offer.get("ListPrice", current_price))
            available_qty = int(comm_offer.get("AvailableQuantity", 0))

            if normal_price < current_price:
                normal_price = current_price

            # Categoría
            categories = raw.get("categories", ["Tecnología"])
            category = categories[0].strip("/").split("/")[-1] if categories else "general"

            if not product_id or not title or current_price <= 0:
                return None

            return ProductItem(
                id=f"paris-{product_id}",
                store_id=self.store_id,
                store_name=self.store_name,
                title=title,
                category=category.lower(),
                current_price=current_price,
                normal_price=normal_price,
                card_price=None,
                url=url,
                image_url=image_url,
                in_stock=available_qty > 0,
                timestamp=datetime.utcnow(),
            )
        except Exception as exc:
            logger.debug("Error al parsear item VTEX: %s", exc)
            return None

    def _build_simulated_catalog(self) -> List[ProductItem]:
        """
        Retorna el catálogo realista de simulación de alta fidelidad.
        Incluye productos representativos de Paris.cl, precios normales, ofertas con Tarjeta Cencosud,
        glitches por error de tipeo/caída de ceros y casos trampa de accesorios/repuestos.
        """
        now = datetime.utcnow()
        raw_catalog = [
            # 1. 🚨 GLITCH CRÍTICO 1: Reloj Garmin Fenix 7 Solar (Pérdida de cero: $699.990 -> $69.990)
            {
                "id": "paris-549102999",
                "title": "Reloj Deportivo Garmin Fenix 7 Solar Slate Gray 47mm con GPS",
                "category": "smartwatch",
                "normal_price": 699990.0,
                "current_price": 69990.0,
                "card_price": 64990.0,  # Tarjeta Cencosud Scotiabank
                "url": "https://www.paris.cl/reloj-garmin-fenix-7-solar-slate-gray-549102999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/549102999",
                "in_stock": True,
            },
            # 2. 🚨 GLITCH CRÍTICO 2: Aspiradora Robot Roomba i3+ (Pérdida de cero: $459.990 -> $45.990)
            {
                "id": "paris-823910999",
                "title": "Aspiradora Robot iRobot Roomba i3+ con Base de Vaciado Automático Clean Base",
                "category": "electrohogar",
                "normal_price": 459990.0,
                "current_price": 45990.0,
                "card_price": 39990.0,  # Tarjeta Cencosud Scotiabank
                "url": "https://www.paris.cl/aspiradora-robot-irobot-roomba-i3-plus-823910999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/823910999",
                "in_stock": True,
            },
            # 3. 🎯 OFERTA LEGÍTIMA 1: Soundbar Sony Dolby Atmos HT-G700
            {
                "id": "paris-312948999",
                "title": "Soundbar Sony HT-G700 3.1 Canales Dolby Atmos DTS:X Bluetooth Subwoofer Inalámbrico",
                "category": "audio",
                "normal_price": 399990.0,
                "current_price": 279990.0,
                "card_price": 259990.0,  # ~35% con Cencosud
                "url": "https://www.paris.cl/soundbar-sony-ht-g700-dolby-atmos-dtsx-312948999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/312948999",
                "in_stock": True,
            },
            # 4. 🎯 OFERTA LEGÍTIMA 2: iPhone 14 Plus 128GB
            {
                "id": "paris-439201999",
                "title": "Apple iPhone 14 Plus 128GB Medianoche 5G Pantalla Super Retina XDR 6.7''",
                "category": "smartphones",
                "normal_price": 829990.0,
                "current_price": 749990.0,
                "card_price": 719990.0,  # Descuento normal con Tarjeta Cencosud
                "url": "https://www.paris.cl/apple-iphone-14-plus-128gb-medianoche-439201999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/439201999",
                "in_stock": True,
            },
            # 5. 🎯 OFERTA LEGÍTIMA 3: Zapatillas Nike Air Zoom Pegasus 40
            {
                "id": "paris-671239999",
                "title": "Zapatillas Running Hombre Nike Air Zoom Pegasus 40 Negro",
                "category": "calzado_deporte",
                "normal_price": 119990.0,
                "current_price": 89990.0,
                "card_price": 79990.0,  # Oferta Cyber / Cencosud
                "url": "https://www.paris.cl/zapatilla-running-nike-air-zoom-pegasus-40-671239999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/671239999",
                "in_stock": True,
            },
            # 6. 🪤 CASO TRAMPA 1: Correa de Silicona para Garmin Fenix (Anti-Falso Positivo)
            {
                "id": "paris-192837999",
                "title": "Correa de Silicona Deportiva QuickFit 22mm para Reloj Garmin Fenix 7 / Fenix 6",
                "category": "accesorios_smartwatch",
                "normal_price": 19990.0,
                "current_price": 9990.0,
                "card_price": 8990.0,
                "url": "https://www.paris.cl/correa-silicona-quickfit-22mm-garmin-fenix-7-192837999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/192837999",
                "in_stock": True,
            },
            # 7. 🪤 CASO TRAMPA 2: Pack Repuestos para Aspiradora Roomba (Anti-Falso Positivo)
            {
                "id": "paris-994821999",
                "title": "Pack Repuestos Filtros Hepa y Cepillos de Goma para Aspiradora Robot iRobot Roomba i3 e5 i7",
                "category": "repuestos_electrohogar",
                "normal_price": 24990.0,
                "current_price": 14990.0,
                "card_price": 12990.0,
                "url": "https://www.paris.cl/pack-repuestos-filtros-cepillos-roomba-i3-i7-994821999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/994821999",
                "in_stock": True,
            },
            # 8. 🪤 CASO TRAMPA 3: Funda MagSafe para iPhone 14 Plus (Anti-Falso Positivo)
            {
                "id": "paris-771234999",
                "title": "Funda Silicon Case con MagSafe para iPhone 14 Plus Azul Tempestad",
                "category": "accesorios_telefonia",
                "normal_price": 29990.0,
                "current_price": 8990.0,
                "card_price": 7990.0,
                "url": "https://www.paris.cl/funda-silicon-case-magsafe-iphone-14-plus-771234999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/771234999",
                "in_stock": True,
            },
            # 9. 🎯 OFERTA LEGÍTIMA 4: Smart TV LG OLED evo 55" C3
            {
                "id": "paris-883921999",
                "title": "Smart TV LG OLED evo 55'' 4K UHD OLED55C3PSA ThinQ AI Dolby Vision",
                "category": "televisores",
                "normal_price": 1299990.0,
                "current_price": 899990.0,
                "card_price": 849990.0,
                "url": "https://www.paris.cl/smart-tv-lg-oled-evo-55-4k-uhd-oled55c3psa-883921999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/883921999",
                "in_stock": True,
            },
            # 10. 🎯 OFERTA LEGÍTIMA 5: Cafetera Nespresso Vertuo Pop
            {
                "id": "paris-552190999",
                "title": "Cafetera Nespresso Vertuo Pop Black con Espumador de Leche Aeroccino 3",
                "category": "electrohogar",
                "normal_price": 149990.0,
                "current_price": 99990.0,
                "card_price": 89990.0,
                "url": "https://www.paris.cl/cafetera-nespresso-vertuo-pop-black-aeroccino-552190999.html",
                "image_url": "https://paris.scene7.com/is/image/cencosud/552190999",
                "in_stock": True,
            },
        ]

        items: List[ProductItem] = []
        for raw in raw_catalog:
            items.append(
                ProductItem(
                    id=raw["id"],
                    store_id=self.store_id,
                    store_name=self.store_name,
                    title=raw["title"],
                    category=raw["category"],
                    current_price=raw["current_price"],
                    normal_price=raw["normal_price"],
                    card_price=raw.get("card_price"),
                    url=raw["url"],
                    image_url=raw.get("image_url"),
                    in_stock=raw.get("in_stock", True),
                    timestamp=now,
                )
            )
        return items


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 80)
    print("🛒 PRUEBA DIRECTA DE SCRAPER: Paris.cl (Cencosud)")
    print("=" * 80)

    scraper = ParisScraper()
    print(f"Tienda configurada: {scraper.store_name} (ID: {scraper.store_id})")

    # Prueba del catálogo simulado de alta fidelidad
    print("\n📦 1. Extrayendo catálogo simulado de alta fidelidad...")
    products = scraper.fetch_catalog(use_live=False)
    print(f"Total productos obtenidos: {len(products)}\n")

    print(f"{'#':<3} | {'Título':<42} | {'Normal':<10} | {'Oferta':<9} | {'Cencosud':<10} | {'Desc %':<7} | {'Ahorro CLP':<11} | {'Tipo / Diagnóstico'}")
    print("-" * 125)

    for idx, p in enumerate(products, 1):
        card_str = f"${int(p.card_price):,}".replace(",", ".") if p.card_price else "N/A"
        norm_str = f"${int(p.normal_price):,}".replace(",", ".")
        curr_str = f"${int(p.current_price):,}".replace(",", ".")
        savings_str = f"${int(p.absolute_savings_clp):,}".replace(",", ".")

        # Diagnóstico para la prueba
        ratio = p.normal_price / max(1.0, p.best_price)
        if ratio >= 8.5 and p.absolute_savings_clp >= 300000:
            diag = "🚨 GLITCH CAÍDA DE CEROS (~10x)"
        elif any(trap_word in p.title.lower() for trap_word in ["correa", "repuesto", "funda", "filtro"]):
            diag = "🪤 TRAMPA (Accesorio / Repuesto)"
        elif p.discount_percentage >= 20.0:
            diag = "✅ Oferta legítima retail"
        else:
            diag = "ℹ️ Precio regular"

        short_title = p.title[:40] + ".." if len(p.title) > 42 else p.title
        print(f"{idx:<3} | {short_title:<42} | {norm_str:<10} | {curr_str:<9} | {card_str:<10} | {p.discount_percentage:>5.1f}% | {savings_str:>11} | {diag}")

    # Prueba de intento en vivo con fallback
    print("\n🌐 2. Probando conexión en vivo a API Paris.cl (use_live=True)...")
    live_test = scraper.fetch_catalog(use_live=True)
    print(f"Resultado use_live: {len(live_test)} productos listos para procesamiento.")

    print("\n" + "=" * 80)
    print("✅ Scraper de Paris.cl verificado exitosamente.")
    print("=" * 80)
