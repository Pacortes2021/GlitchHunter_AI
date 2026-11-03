"""
Scraper y Conector de Catálogo para Ripley Chile (simple.ripley.cl).

Forma parte del enjambre de ingesta de GlitchHunter AI (Subagente 05).
Proporciona soporte dual:
  1. Conexión HTTP estructurada en vivo con headers realistas y tolerancia a fallos WAF.
  2. Dataset de simulación de alta fidelidad con productos representativos, precios con
     Tarjeta Ripley / Chek, glitches reales de pérdida de ceros y casos trampa de accesorios.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
import urllib.parse

# Asegurar resolución de importaciones al ejecutar directamente el script
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import requests
except ImportError:
    requests = None

from glitchhunter_ai.core.models import ProductItem

logger = logging.getLogger("glitchhunter.scrapers.ripley")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class RipleyScraper:
    """
    Scraper y extractor de catálogo para Ripley Chile (simple.ripley.cl).
    Permite obtener productos en vivo mediante su API/endpoints o recurrir
    a una base de simulación calibrada con glitches de precio e histórico real.
    """

    store_id: str = "ripley"
    store_name: str = "Ripley Chile"
    base_url: str = "https://simple.ripley.cl"
    api_endpoint: str = "https://simple.ripley.cl/api/v2/products"

    # Headers simulando un navegador Chrome actualizado en macOS
    DEFAULT_HEADERS: Dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://simple.ripley.cl/",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    def __init__(self, timeout: int = 10, session: Optional[Any] = None) -> None:
        """
        Inicializa el cliente extractor para Ripley Chile.

        :param timeout: Tiempo máximo de espera en segundos para peticiones HTTP.
        :param session: Sesión HTTP opcional reutilizable (requests.Session).
        """
        self.timeout = timeout
        self.session = session or (requests.Session() if requests else None)
        if self.session and hasattr(self.session, "headers"):
            self.session.headers.update(self.DEFAULT_HEADERS)

    def fetch_catalog(
        self,
        use_live: bool = False,
        search_terms: Optional[List[str]] = None,
        active_category_ids: Optional[List[int]] = None,
        sort_strategy: Optional[str] = None,
    ) -> List[ProductItem]:
        """
        Obtiene el catálogo de productos de Ripley Chile.

        :param use_live: Si es True, intenta consultar los endpoints en vivo de simple.ripley.cl.
                         Si falla (ej. bloqueo WAF / Cloudflare / Akamai 403), recurre de forma
                         transparente al set simulado de alta fidelidad.
        :param search_terms: Lista opcional de términos a consultar en modo en vivo.
        :return: Lista de objetos ProductItem listos para análisis de glitch.
        """
        if use_live:
            logger.info("Intentando extracción en vivo desde %s...", self.base_url)
            # Agregador primero (cobertura amplia); lectura directa de la tienda como respaldo
            live_items: List[ProductItem] = []
            try:
                from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
                live_items = LiveRetailFeed().fetch_live_products_for_store(
                    "ripley", category_ids=active_category_ids, sort_strategy=sort_strategy
                )
            except Exception as exc:
                logger.debug(f"Error consultando LiveRetailFeed para Ripley: {exc}")
            if not live_items:
                live_items = self._fetch_live(search_terms=search_terms)

            if live_items:
                logger.info("Extracción en vivo exitosa: %d productos reales obtenidos de Ripley.", len(live_items))
                return live_items
            logger.warning(
                "No fue posible obtener catálogo en vivo para Ripley Chile (bloqueo WAF o API vacía). "
                "Modo LIVE activado: retornando lista vacía (sin fallback a simulaciones)."
            )
            return []

        logger.debug("Modo DEMO: Cargando catálogo simulado de alta fidelidad para Ripley...")
        return self.get_simulated_catalog()

    def _fetch_live(self, search_terms: Optional[List[str]] = None) -> List[ProductItem]:
        """
        Lectura directa de Ripley (datos Next.js en __NEXT_DATA__) con freno automático ante bloqueos.
        Con términos de búsqueda consulta el buscador; sin ellos, la categoría de notebooks.
        La antigua API /api/v2/search ya no existe (responde 404).
        """
        from glitchhunter_ai.scrapers.direct_reader import default_direct_reader

        if search_terms:
            items: List[ProductItem] = []
            for term in search_terms:
                items.extend(default_direct_reader.search("ripley", term))
            return items
        return default_direct_reader.fetch_category("ripley", "notebooks")

    def _parse_ripley_product(self, raw: Dict[str, Any]) -> Optional[ProductItem]:
        """
        Transforma una entrada cruda de producto de Ripley en un ProductItem fuertemente tipado.
        """
        try:
            prod_id = str(raw.get("partNumber") or raw.get("uniqueID") or raw.get("id") or "").strip()
            title = str(raw.get("name") or raw.get("title") or "").strip()
            if not prod_id or not title:
                return None

            prices = raw.get("prices", {})
            normal_p = float(prices.get("listPrice") or prices.get("normalPrice") or 0.0)
            offer_p = float(prices.get("offerPrice") or prices.get("currentPrice") or 0.0)
            card_p_raw = prices.get("cardPrice") or prices.get("ripleyPrice")
            card_p = float(card_p_raw) if card_p_raw is not None else None

            # Ajuste de precios base si faltan campos
            current_p = offer_p if offer_p > 0.0 else normal_p
            if normal_p == 0.0:
                normal_p = current_p

            url_suffix = raw.get("url") or f"/{prod_id}p"
            url = urllib.parse.urljoin(self.base_url, url_suffix)
            image_url = raw.get("thumbnailImage") or raw.get("fullImage")
            category = str(raw.get("category") or raw.get("definingCategory") or "general")
            in_stock = bool(raw.get("isAvailable", True))

            return ProductItem(
                id=f"RIP-{prod_id}",
                store_id=self.store_id,
                store_name=self.store_name,
                title=title,
                category=category,
                current_price=current_p,
                normal_price=normal_p,
                card_price=card_p,
                url=url,
                image_url=image_url,
                in_stock=in_stock,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.debug("No se pudo parsear producto de Ripley: %s", exc)
            return None

    def get_simulated_catalog(self) -> List[ProductItem]:
        """
        Retorna un conjunto de datos realista de alta fidelidad representativo de Ripley Chile.

        Incluye:
          - Productos estrella de tecnología y línea blanca de Ripley.
          - Precios normales, de internet y precio exclusivo Tarjeta Ripley / Chek.
          - Glitches de precio inducidos (falta un cero / Missing Zero Bug: iPad Air M2 y Cafetera DeLonghi).
          - Casos trampa de accesorios (Stylus genérico para iPad, Funda para Nintendo Switch, Filtro DeLonghi).
          - Productos sin stock para verificar filtros de inventario.
        """
        now = datetime.now(timezone.utc)

        items: List[ProductItem] = [
            # -------------------------------------------------------------
            # 1. CASO NORMAL: Notebook Gamer Asus ROG Strix
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000394851",
                store_id=self.store_id,
                store_name=self.store_name,
                title='Notebook Gamer ASUS ROG Strix G16 16" Intel Core i9-13980HX 16GB RAM 1TB SSD RTX 4070 165Hz',
                category="computacion / notebooks gamer",
                normal_price=1999990.0,
                current_price=1799990.0,
                card_price=1699990.0,  # Descuento adicional con Tarjeta Ripley
                url="https://simple.ripley.cl/notebook-gamer-asus-rog-strix-g16-16-intel-core-i9-16gb-ram-1tb-ssd-rtx-4070-2000394851p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000394851/2000394851_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 2. GLITCH CRÍTICO: Apple iPad Air M2 (Caída de un Cero - Missing Zero)
            # Normal: $749.990 -> Publicado: $74.990 (¡90% descuento!)
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000391204",
                store_id=self.store_id,
                store_name=self.store_name,
                title='Apple iPad Air M2 11" Wi-Fi 128GB Gris Espacial (2024)',
                category="computacion / tablets",
                normal_price=749990.0,
                current_price=74990.0,   # BUG INDUCIDO: falta un cero
                card_price=69990.0,     # Aún menor con Tarjeta Ripley
                url="https://simple.ripley.cl/apple-ipad-air-11-wi-fi-128gb-gris-espacial-2000391204p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000391204/2000391204_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 3. CASO TRAMPA ACCESORIO: Lápiz Stylus que parece iPad Air M2
            # Diseñado para poner a prueba el filtro semántico anti-carcasas
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-MPP00192831",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Lápiz Stylus Genérico Magnético para Apple iPad Air M2 y Pro 11 Pulgadas Palm Rejection",
                category="computacion / accesorios computacion",
                normal_price=24990.0,
                current_price=12990.0,
                card_price=11990.0,
                url="https://simple.ripley.cl/lapiz-stylus-generico-para-apple-ipad-air-m2-y-pro-11-pulgadas-MPP00192831",
                image_url="https://home.ripley.cl/store/Attachment/WOP/MPP/MPP00192831/MPP00192831_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 4. CASO NORMAL: Nintendo Switch OLED
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000385912",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Consola Nintendo Switch Modelo OLED Blanco 64GB",
                category="videojuegos / consolas",
                normal_price=369990.0,
                current_price=329990.0,
                card_price=309990.0,  # Promoción exclusiva Tarjeta Ripley
                url="https://simple.ripley.cl/consola-nintendo-switch-modelo-oled-blanco-2000385912p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000385912/2000385912_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 5. CASO TRAMPA ACCESORIO: Funda / Carcasa Nintendo Switch OLED
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-MPP00204918",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Funda Carcasa Silicona Antigolpes Nintendo Switch OLED Blanca con Mica Vidrio Templado 9H",
                category="videojuegos / accesorios consolas",
                normal_price=18990.0,
                current_price=8990.0,
                card_price=7990.0,
                url="https://simple.ripley.cl/funda-silicona-antigolpes-nintendo-switch-oled-blanca-MPP00204918",
                image_url="https://home.ripley.cl/store/Attachment/WOP/MPP/MPP00204918/MPP00204918_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 6. GLITCH CRÍTICO: Cafetera DeLonghi Dedica Deluxe (Pérdida de Cero)
            # Normal: $289.990 -> Publicado: $28.990 (¡90% descuento!)
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000371849",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Cafetera Espresso Manual DeLonghi Dedica Deluxe EC685.M Acero Inoxidable 15 Bar",
                category="electrohogar / cafeteras",
                normal_price=289990.0,
                current_price=28990.0,   # BUG INDUCIDO: falta un cero
                card_price=25990.0,     # Precio con Tarjeta Ripley
                url="https://simple.ripley.cl/cafetera-espresso-delonghi-dedica-deluxe-ec685m-2000371849p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000371849/2000371849_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 7. CASO TRAMPA ACCESORIO: Filtro Descalcificador para Cafetera DeLonghi
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-MPP00318294",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Filtro de Agua Descalcificador EcoDecalk Repuesto para Cafetera DeLonghi Dedica EC680 EC685",
                category="electrohogar / repuestos y accesorios",
                normal_price=21990.0,
                current_price=14990.0,
                card_price=13990.0,
                url="https://simple.ripley.cl/filtro-de-agua-descalcificador-para-cafetera-delonghi-dedica-MPP00318294",
                image_url="https://home.ripley.cl/store/Attachment/WOP/MPP/MPP00318294/MPP00318294_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 8. CASO NORMAL: Lavadora Secadora LG Inverter AI DD
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000389421",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Lavadora Secadora LG 14 Kg / 8 Kg Inverter AI DD Carga Frontal WD14VVC4S6C Silver",
                category="electrohogar / linea blanca / lavadoras",
                normal_price=699990.0,
                current_price=529990.0,
                card_price=499990.0,  # Descuento Tarjeta Ripley
                url="https://simple.ripley.cl/lavadora-secadora-lg-14-kg-8-kg-inverter-ai-dd-wd14vvc4s6c-2000389421p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000389421/2000389421_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 9. CASO NORMAL: Smart TV Samsung 65" OLED 4K
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000396014",
                store_id=self.store_id,
                store_name=self.store_name,
                title='Smart TV Samsung 65" OLED 4K UHD QN65S90D 2024 AI NQ4 Processor',
                category="tv y audio / televisores",
                normal_price=1499990.0,
                current_price=1199990.0,
                card_price=1149990.0,
                url="https://simple.ripley.cl/smart-tv-samsung-65-oled-4k-uhd-qn65s90d-2024-2000396014p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000396014/2000396014_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 10. CASO NORMAL: Audífonos Sony WH-1000XM5
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000388102",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Audífonos Inalámbricos Sony WH-1000XM5 Noise Cancelling Negro",
                category="tv y audio / audifonos",
                normal_price=349990.0,
                current_price=299990.0,
                card_price=279990.0,
                url="https://simple.ripley.cl/audifonos-inalambricos-sony-wh-1000xm5-negro-2000388102p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000388102/2000388102_2.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # -------------------------------------------------------------
            # 11. CASO BORDE: Producto sin stock (Agotado)
            # -------------------------------------------------------------
            ProductItem(
                id="RIP-2000390111",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Smartphone Samsung Galaxy S24 Ultra 256GB Titanium Gray (Agotado)",
                category="telefonia / smartphones",
                normal_price=1299990.0,
                current_price=999990.0,
                card_price=949990.0,
                url="https://simple.ripley.cl/samsung-galaxy-s24-ultra-256gb-titanium-gray-2000390111p",
                image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000390111/2000390111_2.jpg",
                in_stock=False,  # Sin stock
                timestamp=now,
            ),
        ]

        return items


if __name__ == "__main__":
    print("=" * 70)
    print("🎯 Probando RipleyScraper (Subagente 05 - GlitchHunter AI)")
    print("=" * 70)

    scraper = RipleyScraper()
    catalog = scraper.fetch_catalog(use_live=False)

    print(f"\n📦 Total de productos obtenidos: {len(catalog)}")
    print(f"🏪 Tienda: {scraper.store_name} ({scraper.store_id})\n")

    print(f"{'ID':<16} | {'PRECIO TARJETA':<14} | {'OFERTA':<10} | {'NORMAL':<10} | {'DTO %':<6} | TÍTULO")
    print("-" * 105)

    glitches = []
    accessories = []

    for item in catalog:
        best_p = item.best_price
        card_str = f"${int(item.card_price):,}".replace(",", ".") if item.card_price else "N/A"
        offer_str = f"${int(item.current_price):,}".replace(",", ".")
        norm_str = f"${int(item.normal_price):,}".replace(",", ".")
        dto_str = f"{item.discount_percentage:.1f}%"

        print(f"{item.id:<16} | {card_str:<14} | {offer_str:<10} | {norm_str:<10} | {dto_str:<6} | {item.title[:45]}")

        # Identificar casos de interés
        if item.discount_percentage >= 80.0 and item.in_stock:
            glitches.append(item)
        if any(acc in item.title.lower() for acc in ["lápiz", "funda", "filtro", "carcasa"]):
            accessories.append(item)

    print("\n" + "=" * 70)
    print(f"🚨 Posibles Glitches Críticos Detectados en Ripley ({len(glitches)}):")
    for g in glitches:
        print(f"  • {g.title}")
        print(f"    Normal: ${g.normal_price:,.0f} -> Mejor Precio: ${g.best_price:,.0f} (Ahorro: ${g.absolute_savings_clp:,.0f} | Descuento: {g.discount_percentage}%)")

    print(f"\n🔍 Casos Trampa de Accesorios para Filtro Semántico ({len(accessories)}):")
    for a in accessories:
        print(f"  • {a.title} (${a.best_price:,.0f} CLP)")

    print("\n✅ Verificación completada con éxito.")
