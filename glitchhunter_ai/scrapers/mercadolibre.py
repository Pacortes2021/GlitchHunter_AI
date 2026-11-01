"""
Scraper y Simulador de Catálogo para Mercado Libre Chile (MELI).
Módulo de GlitchHunter AI para extracción de productos tech, gaming
y detección de anomalías de precio en Mercado Libre Chile.
"""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional
import urllib.parse

# Permitir ejecución directa como script independiente
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

from glitchhunter_ai.core.models import ProductItem

logger = logging.getLogger(__name__)


class MercadoLibreScraper:
    """
    Scraper y extractor de ofertas para Mercado Libre Chile.
    Soporta extracción HTTP en vivo contra endpoints públicos de MELI
    y un set de datos simulado de alta fidelidad para pruebas y benchmarks.
    """

    store_id: str = "mercadolibre"
    store_name: str = "Mercado Libre Chile"
    base_url: str = "https://www.mercadolibre.cl"
    api_search_url: str = "https://api.mercadolibre.com/sites/MLC/search"

    DEFAULT_USER_AGENT: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    # Búsquedas clave para monitoreo de glitches en tech y gaming
    DEFAULT_QUERIES: List[str] = [
        "playstation 5 slim",
        "samsung galaxy s24 ultra",
        "monitor gamer curvo 27",
        "silla gamer cougar",
        "teclado mecanico hyperx",
    ]

    def __init__(self, request_timeout: int = 10):
        self.request_timeout = request_timeout
        self.session = None
        if REQUESTS_AVAILABLE:
            self.session = requests.Session()
            self.session.headers.update({
                "User-Agent": self.DEFAULT_USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            })

    def fetch_catalog(self, use_live: bool = False) -> List[ProductItem]:
        """
        Obtiene el catálogo de productos a monitorear.

        Args:
            use_live: Si es True, intenta consultar endpoints HTTP reales de Mercado Libre.
                      Si falla por WAF/Cloudflare/bloqueo de bot o si es False, retorna el
                      dataset realista simulado.

        Returns:
            Lista de objetos ProductItem listos para análisis de anomalías.
        """
        if use_live:
            live_items = self._fetch_live_catalog()
            if not live_items:
                try:
                    from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
                    live_items = LiveRetailFeed().fetch_live_products_for_store("mercadolibre")
                except Exception as exc:
                    logger.debug(f"Error consultando LiveRetailFeed para Mercado Libre: {exc}")

            if live_items:
                logger.info("Se obtuvieron %d productos reales en vivo desde Mercado Libre.", len(live_items))
                return live_items
            logger.warning(
                "No se pudieron obtener productos en vivo desde Mercado Libre (bloqueo WAF o error de conexión). "
                "Modo LIVE activado: retornando lista vacía (sin fallback a simulaciones)."
            )
            return []

        logger.debug("Modo DEMO: Cargando catálogo simulado de alta fidelidad para Mercado Libre...")
        return self._get_simulated_catalog()

    def _fetch_live_catalog(self) -> List[ProductItem]:
        """
        Intenta consultar los endpoints públicos de búsqueda o listados web de MELI.
        Maneja fallos de red y bloqueo bot con degradación elegante.
        """
        if not REQUESTS_AVAILABLE or self.session is None:
            logger.warning("Librería 'requests' no disponible para consulta en vivo.")
            return []

        products: List[ProductItem] = []

        for query in self.DEFAULT_QUERIES:
            try:
                # 1. Intentar API pública de búsqueda de Mercado Libre (MLC)
                api_params = {
                    "q": query,
                    "limit": 5,
                }
                resp = self.session.get(
                    self.api_search_url,
                    params=api_params,
                    timeout=self.request_timeout,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    for item_data in data.get("results", []):
                        parsed = self._parse_api_item(item_data, query)
                        if parsed:
                            products.append(parsed)
                    continue

                # 2. Fallback a HTML scraping en listado.mercadolibre.cl
                formatted_query = urllib.parse.quote_plus(query.replace(" ", "-"))
                listado_url = f"https://listado.mercadolibre.cl/{formatted_query}"
                html_resp = self.session.get(listado_url, timeout=self.request_timeout)

                if html_resp.status_code == 200 and "suspicious-traffic" not in html_resp.text:
                    parsed_html_items = self._parse_html_listado(html_resp.text, query)
                    products.extend(parsed_html_items)

            except Exception as exc:
                logger.debug("Error al consultar en vivo '%s': %s", query, exc)
                continue

        return products

    def _parse_api_item(self, item: Dict[str, Any], query: str) -> Optional[ProductItem]:
        """Parsea un resultado retornado por la API JSON de Mercado Libre."""
        try:
            item_id = str(item.get("id", ""))
            title = str(item.get("title", ""))
            price = float(item.get("price", 0.0))
            original_price = item.get("original_price")
            normal_price = float(original_price) if original_price else price
            permalink = str(item.get("permalink", ""))
            thumbnail = str(item.get("thumbnail", ""))

            if not item_id or not title or price <= 0.0:
                return None

            return ProductItem(
                id=f"MLC-{item_id}",
                store_id=self.store_id,
                store_name=self.store_name,
                title=title,
                category=self._infer_category(title, query),
                current_price=price,
                normal_price=normal_price,
                card_price=None,
                url=permalink or f"https://articulo.mercadolibre.cl/{item_id}",
                image_url=thumbnail,
                in_stock=True,
                timestamp=datetime.utcnow(),
            )
        except Exception:
            return None

    def _parse_html_listado(self, html_text: str, query: str) -> List[ProductItem]:
        """Parsea la página HTML de resultados de búsqueda de Mercado Libre."""
        items: List[ProductItem] = []
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            cards = soup.select(".ui-search-result__wrapper, .poly-card, .ui-search-layout__item")
            for idx, card in enumerate(cards[:5]):
                title_el = card.select_one(".ui-search-item__title, .poly-component__title, h2")
                price_el = card.select_one(".andes-money-amount__fraction, .poly-price__current .andes-money-amount__fraction")
                old_price_el = card.select_one(".andes-money-amount--previous .andes-money-amount__fraction")
                link_el = card.select_one("a[href]")
                img_el = card.select_one("img")

                if not title_el or not price_el or not link_el:
                    continue

                raw_price = re.sub(r"[^\d]", "", price_el.get_text(strip=True))
                if not raw_price:
                    continue
                current_price = float(raw_price)

                if old_price_el:
                    raw_old = re.sub(r"[^\d]", "", old_price_el.get_text(strip=True))
                    normal_price = float(raw_old) if raw_old else current_price
                else:
                    normal_price = current_price

                title = title_el.get_text(strip=True)
                url = link_el.get("href", "")
                img_url = img_el.get("src") or img_el.get("data-src") if img_el else None

                items.append(
                    ProductItem(
                        id=f"MLC-HTML-{idx+1}-{abs(hash(url)) % 1000000}",
                        store_id=self.store_id,
                        store_name=self.store_name,
                        title=title,
                        category=self._infer_category(title, query),
                        current_price=current_price,
                        normal_price=normal_price,
                        card_price=None,
                        url=url,
                        image_url=img_url,
                        in_stock=True,
                        timestamp=datetime.utcnow(),
                    )
                )
        except Exception as exc:
            logger.debug("Error parseando HTML de Mercado Libre: %s", exc)

        return items

    def _infer_category(self, title: str, query: str = "") -> str:
        """Determina la categoría estimada a partir del título o query."""
        text = f"{title} {query}".lower()
        if any(w in text for w in ["playstation", "ps5", "consola", "nintendo", "xbox"]):
            return "consolas"
        if any(w in text for w in ["samsung", "galaxy", "iphone", "celular", "smartphone", "xiaomi"]):
            return "smartphones"
        if any(w in text for w in ["monitor", "curvo", "pantalla", "oled", "165hz", "144hz"]):
            return "monitores"
        if any(w in text for w in ["silla", "ergonomica", "butaca", "cougar"]):
            return "sillas_gamer"
        if any(w in text for w in ["teclado", "mouse", "headset", "mecanico", "hyperx"]):
            return "perifericos"
        if any(w in text for w in ["funda", "lamina", "carcasa", "protector", "case", "cable"]):
            return "accesorios"
        return "tecnologia"

    def _get_simulated_catalog(self) -> List[ProductItem]:
        """
        Retorna un conjunto de datos realista de Mercado Libre Chile que incluye:
        1. Artículos legítimos de Tiendas Oficiales con cuotas sin interés y envíos Full.
        2. Glitches críticos reales (ej: omisión de un cero / ~90% descuento en equipos caros).
        3. Casos trampa de accesorios / láminas / fundas para probar los filtros semánticos anti-falso positivo.
        """
        now = datetime.utcnow()

        return [
            # =========================================================================
            # CASOS DE GLITCH CRÍTICO (Bugs reales de precio por omisión de cero)
            # =========================================================================
            ProductItem(
                id="MLC-BUG-001-MONITOR-SAMSUNG-27",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Monitor Gamer Curvo 27 Samsung Odyssey G5 WQHD 165Hz 1ms - Tienda Oficial Samsung (Envío Full Gratis)",
                category="monitores",
                current_price=28990.0,    # BUG: Normal $289.990 publicado a $28.990 (Falta un cero)
                normal_price=289990.0,
                card_price=None,          # 12 cuotas sin interés de $2.415 con Mercado Pago
                url="https://articulo.mercadolibre.cl/MLC-910293847-monitor-gamer-curvo-27-samsung-odyssey-g5-wqhd-165hz",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_789123-MLC74839201928_032024-F.webp",
                in_stock=True,
                timestamp=now,
            ),
            ProductItem(
                id="MLC-BUG-002-SAMSUNG-GALAXY-S24-ULTRA",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Celular Samsung Galaxy S24 Ultra 512GB Titanium Black 12GB RAM - Tienda Oficial Samsung",
                category="smartphones",
                current_price=129990.0,   # BUG: Normal $1.299.990 publicado a $129.990 (Falta un cero)
                normal_price=1299990.0,
                card_price=None,          # 12 cuotas sin interés de $10.832 con Mercado Pago Envíos Full
                url="https://articulo.mercadolibre.cl/MLC-920394857-celular-samsung-galaxy-s24-ultra-512gb-titanium-black",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_612849-MLC74239485012_012024-F.webp",
                in_stock=True,
                timestamp=now,
            ),

            # =========================================================================
            # CASOS TRAMPA / FALSOS POSITIVOS (Láminas, protectores y carcasas baratas)
            # Diseñados para que el detector semántico descarte accesorios a precios bajos.
            # =========================================================================
            ProductItem(
                id="MLC-TRAP-001-LAMINA-HIDROGEL-S24",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Lámina Hidrogel Para Samsung Galaxy S24 Ultra Cobertura Completa Antigolpes Fácil Instalación",
                category="accesorios_celulares",
                current_price=5990.0,
                normal_price=9990.0,
                card_price=None,
                url="https://articulo.mercadolibre.cl/MLC-930495867-lamina-hidrogel-pantalla-galaxy-s24-ultra-antihuellas",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_982341-MLC74102938475_022024-F.webp",
                in_stock=True,
                timestamp=now,
            ),
            ProductItem(
                id="MLC-TRAP-002-FUNDA-SILICONA-PS5",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Funda De Silicona Camuflada Y Grips Antideslizantes Para Joystick Sony PlayStation 5 Dualsense",
                category="accesorios_consolas",
                current_price=4990.0,
                normal_price=8990.0,
                card_price=None,
                url="https://articulo.mercadolibre.cl/MLC-940596877-funda-silicona-joystick-ps5-dualsense-playstation",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_876543-MLC73210984756_122023-F.webp",
                in_stock=True,
                timestamp=now,
            ),
            ProductItem(
                id="MLC-TRAP-003-CARCASA-GALAXY-S24U",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Carcasa Protectora Antigolpes Con Anillo Soporte Para Galaxy S24 Ultra Reforzada",
                category="accesorios_celulares",
                current_price=7990.0,
                normal_price=14990.0,
                card_price=None,
                url="https://articulo.mercadolibre.cl/MLC-950697887-carcasa-antigolpes-galaxy-s24-ultra-anillo-magnetico",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_765432-MLC72345678901_112023-F.webp",
                in_stock=True,
                timestamp=now,
            ),

            # =========================================================================
            # PRODUCTOS LEGÍTIMOS DE TIENDAS OFICIALES (Precios normales y ofertas válidas)
            # =========================================================================
            ProductItem(
                id="MLC-OFICIAL-001-PS5-SLIM",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Consola Sony PlayStation 5 Slim 1TB Standard Con Lector De Disco - Tienda Oficial PlayStation",
                category="consolas",
                current_price=499990.0,
                normal_price=549990.0,
                card_price=None,          # 12x $41.665 sin interés, Envío Full Gratis
                url="https://articulo.mercadolibre.cl/MLC-960798897-consola-sony-playstation-5-slim-1tb-standard-oficial",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_912345-MLC71234567890_102023-F.webp",
                in_stock=True,
                timestamp=now,
            ),
            ProductItem(
                id="MLC-OFICIAL-002-SILLA-COUGAR",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Silla Gamer Cougar Armor One Black Ergonómica Reclinable 180° - Tienda Oficial Cougar",
                category="sillas_gamer",
                current_price=169990.0,
                normal_price=189990.0,
                card_price=None,          # 6x $28.331 sin interés, Envío Full
                url="https://articulo.mercadolibre.cl/MLC-970899907-silla-gamer-cougar-armor-one-black-ergonomica",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_823456-MLC70123456789_092023-F.webp",
                in_stock=True,
                timestamp=now,
            ),
            ProductItem(
                id="MLC-OFICIAL-003-GALAXY-S24U-NORMAL",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Celular Samsung Galaxy S24 Ultra 256GB Titanium Gray 12GB RAM - Tienda Oficial Samsung",
                category="smartphones",
                current_price=1199990.0,
                normal_price=1299990.0,
                card_price=None,          # 12x $99.999 sin interés, Envío Full Gratis
                url="https://articulo.mercadolibre.cl/MLC-980910917-celular-samsung-galaxy-s24-ultra-256gb-titanium-gray",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_734567-MLC69012345678_082023-F.webp",
                in_stock=True,
                timestamp=now,
            ),
            ProductItem(
                id="MLC-OFICIAL-004-TECLADO-HYPERX",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Teclado Mecánico HyperX Alloy Origins Core RGB Switch Red Español - Tienda Oficial HyperX",
                category="perifericos",
                current_price=74990.0,
                normal_price=89990.0,
                card_price=None,          # 6x $12.498 sin interés, Envío Full
                url="https://articulo.mercadolibre.cl/MLC-990121927-teclado-mecanico-hyperx-alloy-origins-core-rgb-red",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_645678-MLC68901234567_072023-F.webp",
                in_stock=True,
                timestamp=now,
            ),
            ProductItem(
                id="MLC-OFICIAL-005-MONITOR-ASUS-27",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Monitor Gamer Curvo ASUS TUF Gaming VG27VH1B 27 FHD 165Hz 1ms - Tienda Oficial ASUS",
                category="monitores",
                current_price=229990.0,
                normal_price=249990.0,
                card_price=None,          # 6x $38.331 sin interés, Envío Full Gratis
                url="https://articulo.mercadolibre.cl/MLC-901232937-monitor-gamer-curvo-asus-tuf-gaming-27-fhd-165hz",
                image_url="https://http2.mlstatic.com/D_NQ_NP_2X_556789-MLC67890123456_062023-F.webp",
                in_stock=True,
                timestamp=now,
            ),
        ]


def main() -> None:
    """Función de prueba interactiva para verificar el scraper de Mercado Libre."""
    print("=" * 80)
    print(" 🛒 GLITCHHUNTER AI - SCRAPER MERCADO LIBRE CHILE (MELI)")
    print("=" * 80)

    scraper = MercadoLibreScraper()
    print(f"Tienda: {scraper.store_name} (ID: {scraper.store_id})")
    print(f"Base URL: {scraper.base_url}")
    print("\n[+] Extrayendo catálogo simulado con casos reales de prueba...")

    catalog = scraper.fetch_catalog(use_live=False)
    print(f"[✓] Total de productos cargados: {len(catalog)}\n")

    print("-" * 80)
    print(f"{'ID':<18} | {'PRECIO':>11} | {'NORMAL':>11} | {'DCTO':>7} | {'TÍTULO'}")
    print("-" * 80)

    anomalies = []
    traps = []

    for item in catalog:
        price_str = f"${int(item.current_price):,}".replace(",", ".")
        normal_str = f"${int(item.normal_price):,}".replace(",", ".")
        dcto_str = f"{item.discount_percentage:.1f}%"

        # Detección simple para demostración en consola
        is_missing_zero = False
        if item.current_price > 0:
            ratio = item.normal_price / item.current_price
            if 8.5 <= ratio <= 11.5:
                is_missing_zero = True

        flag = ""
        if is_missing_zero and item.discount_percentage >= 85.0:
            flag = "🚨 ¡GLITCH ZERO!"
            anomalies.append(item)
        elif item.discount_percentage >= 65.0:
            flag = "⚠️ DCTO ALTO"
        elif "lámina" in item.title.lower() or "funda" in item.title.lower() or "carcasa" in item.title.lower():
            flag = "🛡️ ACCESORIO"
            traps.append(item)

        short_title = (item.title[:42] + "..") if len(item.title) > 44 else item.title
        print(f"{item.id:<18} | {price_str:>11} | {normal_str:>11} | {dcto_str:>7} | {short_title} {flag}")

    print("-" * 80)
    print(f"\n📊 RESUMEN DE DIAGNÓSTICO:")
    print(f"  • Glitches Críticos Detectados (Falta de Cero): {len(anomalies)}")
    for a in anomalies:
        saving = f"${int(a.absolute_savings_clp):,}".replace(",", ".")
        print(f"    - [{a.id}] {a.title}")
        print(f"      Precio: ${int(a.current_price):,} vs ${int(a.normal_price):,} (Ahorro: {saving} CLP, -{a.discount_percentage}%)")

    print(f"\n  • Casos Trampa para Análisis Semántico (Láminas/Fundas): {len(traps)}")
    for t in traps:
        print(f"    - [{t.id}] {t.title} (${int(t.current_price):,})")

    print("\n[✓] Scraper de Mercado Libre ejecutado exitosamente sin errores.")
    print("=" * 80)


if __name__ == "__main__":
    main()
