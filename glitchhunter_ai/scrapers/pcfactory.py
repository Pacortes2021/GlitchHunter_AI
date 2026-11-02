"""
Módulo Scraper para PC Factory (Tech & Hardware Especializado) - GlitchHunter AI.

Extrae catálogos de componentes de hardware crítico, computación y gaming de PC Factory
(pcfactory.cl). Ofrece soporte para precios de referencia/lista, precio normal con crédito
y precio preferencial en efectivo o transferencia bancaria.
Incluye capacidad de consulta HTTP estructurada a la API de catálogo de PC Factory
y un simulador de alta fidelidad con errores de precios reales (pérdida de ceros en GPUs,
CPUs, SSDs) y casos trampa de repuestos y accesorios.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
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

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from glitchhunter_ai.core.models import ProductItem

logger = logging.getLogger("glitchhunter.scrapers.pcfactory")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [PCFactoryScraper] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class PCFactoryScraper:
    """
    Scraper y extractor de ofertas para PC Factory (pcfactory.cl).
    Especializado en componentes críticos de hardware: GPUs, procesadores,
    almacenamiento SSD, memorias RAM, placas madre y notebooks gamer.
    """

    store_id: str = "pcfactory"
    store_name: str = "PC Factory"
    base_url: str = "https://www.pcfactory.cl"
    api_base_url: str = "https://api.pcfactory.cl/pcfactory-services-catalogo/v1/catalogo"

    # Pool de SKUs representativos de hardware y tecnología activa en PC Factory
    DEFAULT_SEED_PRODUCT_IDS: List[int] = [
        57841, 54421, 58198, 55221, 57124, 52523, 54806,
        56693, 55617, 58280, 53810, 57172, 55331, 58084,
        57189, 58305, 58512, 56531, 54507, 46284, 53878
    ]

    # Headers estándar emulando un navegador moderno chileno
    DEFAULT_HEADERS: Dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.pcfactory.cl/",
        "Origin": "https://www.pcfactory.cl",
    }

    def __init__(self, timeout: float = 6.0) -> None:
        """
        Inicializa el scraper con timeout y parámetros de conexión.
        """
        self.timeout = timeout

    def fetch_catalog(self, use_live: bool = False) -> List[ProductItem]:
        """
        Punto de entrada principal para obtener el catálogo de productos de PC Factory.

        Args:
            use_live: Si es True, intenta consultar la API o endpoints en vivo de PC Factory.
                      Si falla, hay timeout o bloqueo, ejecuta un fallback seguro al catálogo simulado.
                      Si es False, retorna directamente el catálogo simulado de alta fidelidad.

        Returns:
            Lista de objetos ProductItem listos para el motor de detección de glitches.
        """
        if use_live:
            logger.info("Modo LIVE activado. Consultando catálogo en vivo de PC Factory...")
            live_items = self.fetch_live_catalog()
            if not live_items:
                try:
                    from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
                    live_items = LiveRetailFeed().fetch_live_products_for_store("pcfactory")
                except Exception as exc:
                    logger.debug(f"Error consultando LiveRetailFeed para PC Factory: {exc}")

            if live_items:
                logger.info("Éxito en vivo: Se obtuvieron %d productos reales de PC Factory", len(live_items))
                return live_items
            logger.warning(
                "Fallo en consulta en vivo o bloqueo de red en PC Factory. "
                "Modo LIVE activado: retornando lista vacía (sin fallback a simulaciones)."
            )
            return []

        logger.debug("Modo DEMO: Cargando catálogo simulado de alta fidelidad para PC Factory...")
        return self.get_simulated_catalog()

    def fetch_live_catalog(self, product_ids: Optional[List[int]] = None) -> List[ProductItem]:
        """
        Realiza consulta HTTP estructurada al microservicio de catálogo de PC Factory.
        Utiliza el endpoint `/productos/banners?productsIds=...` con datos completos de precios.
        """
        ids_to_query = product_ids or self.DEFAULT_SEED_PRODUCT_IDS
        ids_param = ",".join(str(pid) for pid in ids_to_query)
        endpoint = f"{self.api_base_url}/productos/banners"
        params = {"productsIds": ids_param}

        raw_data: Optional[List[Dict[str, Any]]] = None

        if HAS_HTTPX:
            try:
                with httpx.Client(headers=self.DEFAULT_HEADERS, timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.get(endpoint, params=params)
                    if resp.status_code == 200:
                        raw_data = resp.json()
                    else:
                        logger.warning("HTTP %d al consultar API PC Factory: %s", resp.status_code, resp.text[:120])
            except Exception as exc:
                logger.warning("Excepción con httpx al consultar PC Factory: %s", exc)

        if raw_data is None and HAS_REQUESTS:
            try:
                resp = requests.get(endpoint, params=params, headers=self.DEFAULT_HEADERS, timeout=self.timeout)
                if resp.status_code == 200:
                    raw_data = resp.json()
                else:
                    logger.warning("HTTP %d (requests) al consultar API PC Factory", resp.status_code)
            except Exception as exc:
                logger.warning("Excepción con requests al consultar PC Factory: %s", exc)

        if not raw_data or not isinstance(raw_data, list):
            return []

        return self.parse_pcfactory_json(raw_data)

    def parse_pcfactory_json(self, raw_products: List[Dict[str, Any]]) -> List[ProductItem]:
        """
        Parsea la respuesta JSON estructurada de la API de PC Factory a entidades ProductItem.
        """
        items: List[ProductItem] = []
        now = datetime.now(timezone.utc)

        for raw in raw_products:
            try:
                pid = str(raw.get("id", ""))
                if not pid:
                    continue

                nombre = raw.get("nombre") or raw.get("descripcionCorta") or "Producto PC Factory"
                familia = raw.get("familia", {})
                cat_name = familia.get("nombre") if isinstance(familia, dict) else "hardware"
                category = f"hardware / {str(cat_name).lower()}"

                # Estructura de precios en PC Factory
                # 'referencia': precio lista / tachado
                # 'normal': precio con tarjetas de crédito
                # 'efectivo': precio pago transferencia o efectivo (suele ser el más económico)
                precio_info = raw.get("precio") or {}
                ref_price = float(precio_info.get("referencia") or 0.0)
                norm_price = float(precio_info.get("normal") or 0.0)
                efec_price = float(precio_info.get("efectivo") or 0.0)

                # Resolver precio normal de referencia
                normal_p = ref_price if ref_price > 0.0 else (norm_price if norm_price > 0.0 else efec_price)
                # Resolver precio de venta actual (crédito)
                current_p = norm_price if norm_price > 0.0 else (efec_price if efec_price > 0.0 else normal_p)
                # Resolver precio preferencial (efectivo / transferencia como card_price)
                card_p = efec_price if (0.0 < efec_price < current_p) else None

                if current_p <= 0.0:
                    continue

                # Extraer imagen principal
                image_url: Optional[str] = None
                imagenes = raw.get("imagenes") or {}
                if isinstance(imagenes, dict) and imagenes:
                    first_img = imagenes.get("0") or next(iter(imagenes.values()), None)
                    if isinstance(first_img, dict):
                        image_url = first_img.get("250") or first_img.get("500") or next(iter(first_img.values()), None)
                    elif isinstance(first_img, str):
                        image_url = first_img

                product_url = f"{self.base_url}/producto/{pid}"

                items.append(
                    ProductItem(
                        id=f"pcfactory_{pid}",
                        store_id=self.store_id,
                        store_name=self.store_name,
                        title=nombre,
                        category=category,
                        current_price=current_p,
                        normal_price=normal_p,
                        card_price=card_p,
                        url=product_url,
                        image_url=image_url,
                        in_stock=True,
                        timestamp=now,
                    )
                )
            except Exception as e:
                logger.debug("Error parseando producto individual de PC Factory: %s", e)

        return items

    def get_simulated_catalog(self) -> List[ProductItem]:
        """
        Genera un catálogo de simulación de alta fidelidad representativo de PC Factory y tiendas tech.

        Incluye:
        1. Casos críticos de glitch de precios en hardware (caída de ceros ~10x):
           - GPU NVIDIA RTX 4080 Super de $1.199.990 a $119.990
           - CPU AMD Ryzen 7 7800X3D de $449.990 a $44.990
           - SSD NVMe 2TB Samsung 990 Pro de $189.990 a $18.990
           - Notebook Gamer Lenovo Legion Pro 5 de $1.799.990 a $279.990
        2. Componentes estándar con estructura de precios normal, crédito y efectivo/transferencia:
           - Memoria RAM 32GB DDR5 Corsair Vengeance
           - Monitor ASUS ROG Swift OLED 27"
           - Placa Madre ASUS ROG Strix B650E-F
           - Fuente de Poder Corsair RM1000x Gold
        3. Casos trampa de repuestos y accesorios (para pruebas rigurosas del filtro semántico IA):
           - Cable HDMI 2.1 8K para RTX 4080 ($11.990)
           - Pasta Térmica Arctic MX-4 para AMD Ryzen ($6.990)
           - Soporte Vertical Anti-Sag para Tarjeta de Video RTX 4080 ($12.990)
           - Filtro Antipolvo Magnético 120mm ($3.990)
           - Adaptador 12VHPWR 16 pines a 3x8 pines PCIe ($14.990)
        4. Casos de precio inflado (Cyber Bait) y producto agotado sin stock.
        """
        now = datetime.now(timezone.utc)

        simulated_data: List[ProductItem] = [
            # 🚨 GLITCH CRÍTICO 1: Tarjeta de Video NVIDIA RTX 4080 Super (Falta un cero: $1.199.990 -> $119.990)
            ProductItem(
                id="PCF-SKU-48921",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Tarjeta de Video NVIDIA GeForce RTX 4080 Super Gaming OC 16GB GDDR6X",
                category="hardware / tarjetas de video",
                current_price=119990.0,
                normal_price=1199990.0,
                card_price=109990.0,  # Precio Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/48921-tarjeta-de-video-geforce-rtx-4080-super-gaming-oc-16gb",
                image_url="https://assets.pcfactory.cl/public/foto/48921/rtx4080super.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🚨 GLITCH CRÍTICO 2: Procesador AMD Ryzen 7 7800X3D (Falta un cero: $449.990 -> $44.990)
            ProductItem(
                id="PCF-SKU-50182",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Procesador AMD Ryzen 7 7800X3D 8-Core 4.2GHz (5.0GHz Turbo) Socket AM5",
                category="hardware / procesadores",
                current_price=44990.0,
                normal_price=449990.0,
                card_price=42990.0,  # Precio Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/50182-procesador-amd-ryzen-7-7800x3d-am5",
                image_url="https://assets.pcfactory.cl/public/foto/50182/ryzen7800x3d.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🚨 GLITCH CRÍTICO 3: Unidad SSD NVMe 2TB Samsung 990 Pro (Falta un cero: $189.990 -> $18.990)
            ProductItem(
                id="PCF-SKU-49823",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Unidad SSD 2TB PCIe 4.0 NVMe M.2 990 PRO con Disipador Térmico Samsung",
                category="hardware / almacenamiento ssd",
                current_price=18990.0,
                normal_price=189990.0,
                card_price=17990.0,  # Precio Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/49823-ssd-2tb-pcie-4-0-nvme-m2-990-pro-heatsink",
                image_url="https://assets.pcfactory.cl/public/foto/49823/samsung990pro.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🚨 GLITCH CRÍTICO 4: Notebook Gamer Lenovo Legion Pro 5 (Descuento anómalo masivo ~84%)
            ProductItem(
                id="PCF-SKU-53891",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Notebook Gamer Lenovo Legion Pro 5 16\" WQXGA Intel Core i7-14700HX 32GB 1TB SSD RTX 4070 165Hz",
                category="computacion / notebooks gamer",
                current_price=279990.0,
                normal_price=1799990.0,
                card_price=269990.0,  # Precio Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/53891-notebook-gamer-legion-pro-5-i7-32gb-1tb-rtx4070",
                image_url="https://assets.pcfactory.cl/public/foto/53891/legionpro5.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ⚠️ CASO TRAMPA 1 (Accesorio de GPU): Cable HDMI 2.1 8K para RTX 4080 ($11.990)
            ProductItem(
                id="PCF-SKU-31201",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Cable HDMI 2.1 8K Ultra High Speed 48Gbps 2m para RTX 4080 / PS5 / Xbox Series X",
                category="cables y adaptadores",
                current_price=11990.0,
                normal_price=19990.0,
                card_price=10990.0,
                url="https://www.pcfactory.cl/producto/31201-cable-hdmi-2-1-8k-48gbps-2m",
                image_url="https://assets.pcfactory.cl/public/foto/31201/cablehdmi21.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ⚠️ CASO TRAMPA 2 (Consumible / Repuesto de CPU): Pasta Térmica Arctic MX-4 ($6.990)
            ProductItem(
                id="PCF-SKU-28491",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Pasta Térmica Arctic MX-4 4g de Alto Rendimiento para CPU AMD Ryzen / Intel",
                category="refrigeracion / pastas termicas",
                current_price=6990.0,
                normal_price=9990.0,
                card_price=5990.0,
                url="https://www.pcfactory.cl/producto/28491-pasta-termica-arctic-mx-4-4g",
                image_url="https://assets.pcfactory.cl/public/foto/28491/arcticmx4.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ⚠️ CASO TRAMPA 3 (Accesorio de Soporte): Bracket Anti-Sag para Tarjeta de Video ($12.990)
            ProductItem(
                id="PCF-SKU-44182",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Soporte Vertical Regulable Anti-Sag Bracket con Base Imantada para Tarjeta de Video RTX 4080 / 4090",
                category="accesorios hardware",
                current_price=12990.0,
                normal_price=18990.0,
                card_price=11990.0,
                url="https://www.pcfactory.cl/producto/44182-soporte-anti-sag-gpu-aluminio",
                image_url="https://assets.pcfactory.cl/public/foto/44182/antisagbracket.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ⚠️ CASO TRAMPA 4 (Filtro Gabinete): Filtro Antipolvo Magnético 120mm ($3.990)
            ProductItem(
                id="PCF-SKU-18293",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Filtro Magnético Antipolvo PVC 120mm Malla Fina para Gabinete Gamer",
                category="accesorios gabinete",
                current_price=3990.0,
                normal_price=6990.0,
                card_price=3490.0,
                url="https://www.pcfactory.cl/producto/18293-filtro-magnetico-antipolvo-120mm",
                image_url="https://assets.pcfactory.cl/public/foto/18293/filtroantipolvo.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ⚠️ CASO TRAMPA 5 (Cable Adaptador): Adaptador 12VHPWR para RTX 4080 ($14.990)
            ProductItem(
                id="PCF-SKU-47391",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Adaptador 12VHPWR PCIe 5.0 16 pines a 3x8 pines para NVIDIA RTX 4080 / 4090",
                category="cables y adaptadores",
                current_price=14990.0,
                normal_price=21990.0,
                card_price=13990.0,
                url="https://www.pcfactory.cl/producto/47391-adaptador-12vhpwr-16pin-a-3x8pin",
                image_url="https://assets.pcfactory.cl/public/foto/47391/adaptador12vhpwr.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ✅ OFERTA LEGÍTIMA 1: Memoria RAM 32GB DDR5 Corsair Vengeance (Descuento normal de tienda ~15%)
            ProductItem(
                id="PCF-SKU-51294",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Memoria RAM 32GB (2x16GB) DDR5 6000MHz Vengeance RGB AMD Expo Corsair",
                category="hardware / memorias ram",
                current_price=129990.0,
                normal_price=149990.0,
                card_price=124990.0,  # Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/51294-ram-32gb-2x16gb-ddr5-6000mhz-vengeance-rgb",
                image_url="https://assets.pcfactory.cl/public/foto/51294/corsairram.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ✅ OFERTA LEGÍTIMA 2: Monitor Gamer ASUS ROG Swift 27" OLED QHD 240Hz
            ProductItem(
                id="PCF-SKU-49120",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Monitor Gamer ASUS ROG Swift 27\" OLED QHD 240Hz 0.03ms PG27AQDM",
                category="computacion / monitores gamer",
                current_price=899990.0,
                normal_price=999990.0,
                card_price=869990.0,  # Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/49120-monitor-asus-rog-swift-oled-27-240hz",
                image_url="https://assets.pcfactory.cl/public/foto/49120/asusrogmonitor.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ✅ OFERTA LEGÍTIMA 3: Placa Madre ASUS ROG Strix B650E-F Gaming WiFi
            ProductItem(
                id="PCF-SKU-48195",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Placa Madre ASUS ROG Strix B650E-F Gaming WiFi Socket AM5",
                category="hardware / placas madre",
                current_price=299990.0,
                normal_price=329990.0,
                card_price=289990.0,  # Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/48195-placa-madre-asus-rog-strix-b650e-f-wifi",
                image_url="https://assets.pcfactory.cl/public/foto/48195/asusrogstrixb650e.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # ✅ OFERTA LEGÍTIMA 4: Fuente de Poder Corsair RM1000x 1000W 80 Plus Gold
            ProductItem(
                id="PCF-SKU-46820",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Fuente de Poder Corsair RM1000x 1000W 80 Plus Gold Fully Modular ATX 3.0",
                category="hardware / fuentes de poder",
                current_price=169990.0,
                normal_price=189990.0,
                card_price=164990.0,  # Efectivo / Transferencia
                url="https://www.pcfactory.cl/producto/46820-fuente-corsair-rm1000x-1000w-gold",
                image_url="https://assets.pcfactory.cl/public/foto/46820/corsairrm1000x.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🔍 CASO PRECIO INFLADO (Cyber Bait): Mouse Logitech G502 con precio de referencia inflado
            ProductItem(
                id="PCF-SKU-38102",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Mouse Gamer Óptico Logitech G502 HERO High Performance 25K DPI",
                category="accesorios gaming / mouse",
                current_price=39990.0,
                normal_price=89990.0,  # Precio de lista inflado artificialmente
                card_price=36990.0,
                url="https://www.pcfactory.cl/producto/38102-mouse-gamer-logitech-g502-hero",
                image_url="https://assets.pcfactory.cl/public/foto/38102/logitechg502.jpg",
                in_stock=True,
                timestamp=now,
            ),

            # 🚫 CASO AGOTADO (Sin Stock): GPU RTX 4090 24GB ASUS ROG Strix
            ProductItem(
                id="PCF-SKU-47901",
                store_id=self.store_id,
                store_name=self.store_name,
                title="Tarjeta de Video ASUS ROG Strix GeForce RTX 4090 24GB GDDR6X OC Edition",
                category="hardware / tarjetas de video",
                current_price=2099990.0,
                normal_price=2299990.0,
                card_price=1999990.0,
                url="https://www.pcfactory.cl/producto/47901-tarjeta-video-asus-rog-strix-rtx-4090-24gb-oc",
                image_url="https://assets.pcfactory.cl/public/foto/47901/asusrtx4090strix.jpg",
                in_stock=False,  # Agotado sin unidades disponibles
                timestamp=now,
            ),
        ]

        return simulated_data


if __name__ == "__main__":
    print("=" * 76)
    print("🖥️  GlitchHunter AI - Prueba de Scraper: PC Factory (Tech & Hardware)")
    print("=" * 76)

    scraper = PCFactoryScraper()
    print(f"Tienda configurada : {scraper.store_name} ({scraper.store_id})")
    print(f"API Base URL       : {scraper.api_base_url}")

    # 1. Prueba de Catálogo Simulado
    print("\n" + "-" * 76)
    print("📦 MODO SIMULADO: Evaluando catálogo especializado de hardware")
    print("-" * 76)
    catalog = scraper.fetch_catalog(use_live=False)
    print(f"Total productos en catálogo simulado: {len(catalog)}\n")

    for idx, item in enumerate(catalog, 1):
        best_p = item.best_price
        disc = item.discount_percentage
        savings = item.absolute_savings_clp
        card_str = f" | Efectivo/Transf: ${item.card_price:,.0f}" if item.card_price else ""
        stock_str = "🟢 STOCK" if item.in_stock else "🔴 SIN STOCK"

        tag = "  "
        if disc >= 80.0 and item.in_stock:
            tag = "🚨 [GLITCH DETECTADO]"
        elif "cable" in item.title.lower() or "pasta" in item.title.lower() or "soporte" in item.title.lower():
            tag = "⚠️ [CASO TRAMPA]"

        print(f"[{idx:02d}] {tag} {item.title}")
        print(f"     SKU: {item.id} | Cat: {item.category} | {stock_str}")
        print(f"     Normal: ${item.normal_price:,.0f} | Oferta: ${item.current_price:,.0f}{card_str}")
        print(f"     Mejor Precio: ${best_p:,.0f} -> Descuento: {disc:.1f}% | Ahorro: ${savings:,.0f} CLP")
        print(f"     URL: {item.url}")
        print("-" * 76)

    # 2. Prueba Rápida de Conexión Live (si la red lo permite)
    print("\n" + "-" * 76)
    print("🌐 MODO LIVE: Prueba de consulta a API en vivo de PC Factory")
    print("-" * 76)
    try:
        live_catalog = scraper.fetch_catalog(use_live=True)
        print(f"Resultado Live: {len(live_catalog)} productos extraídos de la API real de PC Factory.")
        if live_catalog:
            sample = live_catalog[0]
            print(f"Muestra en vivo: {sample.title} | Precio: ${sample.best_price:,.0f} CLP (Normal: ${sample.normal_price:,.0f})")
    except Exception as exc:
        print(f"Nota: Consulta Live falló o fue omitida: {exc}")
    print("=" * 76)
