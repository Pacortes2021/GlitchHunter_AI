"""
Módulo Scraper de SoloTodo Meta-Retailer para GlitchHunter AI.
Monitorea en tiempo real precios consolidados en decenas de tiendas chilenas
(Falabella, Ripley, Paris, PC Factory, SP Digital, Digitek, etc.)
utilizando consultas paralelas con curl_cffi para latencia ultrabaja (<1.5s).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.scrapers.stealth_client import default_stealth_client

logger = logging.getLogger("glitchhunter.scrapers.solotodo")

# Mapeo de categorías clave de hardware y tecnología en SoloTodo
TARGET_CATEGORIES = {
    1: "notebooks",
    2: "componentes",     # Tarjetas de video
    4: "monitores",
    6: "smartphones",
    11: "televisores",
    33: "consolas",
    50: "audio",
}


def _infer_category_name(cat_id: int, title: str) -> str:
    if cat_id in TARGET_CATEGORIES:
        return TARGET_CATEGORIES[cat_id]
    t = title.lower()
    if any(k in t for k in ("funda", "carcasa", "case", "mica", "cable", "adaptador")):
        return "accesorios"
    if any(k in t for k in ("macbook", "notebook", "laptop")):
        return "notebooks"
    if any(k in t for k in ("rtx", "geforce", "radeon", "gpu", "tarjeta de video")):
        return "componentes"
    return "tecnologia"


class SoloTodoScraper:
    """
    Scraper Meta-Retailer para SoloTodo Chile.
    Accede al catálogo consolidado de múltiples tiendas con concurrencia paralela
    y cliente sigiloso para evitar cualquier bloqueo o retraso.
    """

    def __init__(self, timeout: float = 6.0, max_workers: int = 4):
        self.store_id = "solotodo"
        self.store_name = "SoloTodo Meta-Retailer"
        self.timeout = timeout
        self.max_workers = max_workers
        self.client = default_stealth_client
        self.last_latency_ms: float = 0.0

    def _fetch_category_batch(
        self,
        cat_id: int,
        limit: int = 10,
        ordering: str = "offer_price_usd",
    ) -> List[ProductItem]:
        """Consulta una categoría individual en la API de SoloTodo con ordenamiento configurable."""
        url = f"https://publicapi.solotodo.com/products/browse/?categories={cat_id}&page_size={limit}&ordering={ordering}"
        items: List[ProductItem] = []
        try:
            resp = self.client.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                logger.debug("SoloTodo API respondió HTTP %s para categoría %s", resp.status_code, cat_id)
                return []

            data = resp.json()
            results = data.get("results", [])

            # Para los primeros resultados, obtener entidades activas en tiendas
            for entry in results:
                product_entries = entry.get("product_entries", [])
                if not product_entries:
                    continue

                pe = product_entries[0]
                prod = pe.get("product", {})
                metadata = pe.get("metadata", {})
                prices = metadata.get("prices_per_currency", [])

                if not prod or not prices:
                    continue

                curr_price = float(prices[0].get("offer_price", 0.0))
                norm_price = float(prices[0].get("normal_price", 0.0))
                if curr_price <= 0:
                    continue
                if norm_price <= 0:
                    norm_price = curr_price

                prod_id = f"solotodo-{prod.get('id')}"
                title = prod.get("name", "Producto SoloTodo").strip()
                slug = prod.get("slug", "")
                prod_url = f"https://www.solotodo.cl/products/{prod.get('id')}-{slug}" if slug else f"https://www.solotodo.cl/products/{prod.get('id')}"
                img_url = prod.get("picture_url")
                cat_name = _infer_category_name(cat_id, title)

                items.append(
                    ProductItem(
                        id=prod_id,
                        store_id=self.store_id,
                        store_name=self.store_name,
                        title=title,
                        category=cat_name,
                        current_price=curr_price,
                        normal_price=norm_price,
                        card_price=None,
                        url=prod_url,
                        image_url=img_url,
                        in_stock=True,
                        timestamp=datetime.utcnow(),
                    )
                )
        except Exception as exc:
            logger.debug("Error extrayendo categoría %s de SoloTodo: %s", cat_id, exc)

        return items

    def fetch_catalog(
        self,
        use_live: bool = False,
        max_items: int = 50,
        active_category_ids: Optional[List[int]] = None,
        sort_strategy: Optional[str] = None,
    ) -> List[ProductItem]:
        """
        Obtiene el catálogo de SoloTodo.
        En modo live, ejecuta consultas en paralelo para garantizar latencia idéntica
        o superior a los scrapers individuales, con soporte para categorías activas rotativas
        y ordenamiento por mayor descuento o menor precio.
        """
        start_time = time.perf_counter()

        if not use_live:
            self.last_latency_ms = 5.0
            return self.get_simulated_catalog()

        # En modo LIVE: consultas concurrentes vía ThreadPoolExecutor
        products: List[ProductItem] = []
        cat_ids = active_category_ids or list(TARGET_CATEGORIES.keys())[:5]
        per_cat = max(3, max_items // max(1, len(cat_ids)))
        ordering = "-discount_percentage" if sort_strategy == "discount_desc" else "offer_price_usd"

        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_cat = {
                    executor.submit(self._fetch_category_batch, cat_id, per_cat, ordering): cat_id
                    for cat_id in cat_ids
                }
                for future in as_completed(future_to_cat):
                    try:
                        res = future.result()
                        if res:
                            products.extend(res)
                    except Exception as err:
                        logger.debug("Excepción en batch de SoloTodo: %s", err)

        except Exception as e:
            logger.warning("Fallo en extracción LIVE de SoloTodo: %s", e)

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        self.last_latency_ms = round(elapsed_ms, 2)
        logger.info("SoloTodo escaneo completado en %.1f ms (%d productos)", self.last_latency_ms, len(products))
        return products[:max_items]

    def get_simulated_catalog(self) -> List[ProductItem]:
        """Catálogo simulado para pruebas de integración y benchmarks."""
        now = datetime.utcnow()
        return [
            # Glitch crítico genuino (~90% off - Missing Zero Bug)
            ProductItem(
                id="solotodo-glitch-nb-001",
                store_id="solotodo",
                store_name="SoloTodo Meta-Retailer",
                title="Notebook Gamer ASUS ROG Strix SCAR 16 (i9-13980HX / 32GB / RTX 4080 12GB)",
                category="notebooks",
                current_price=279990.0,
                normal_price=2799990.0,
                card_price=None,
                url="https://www.solotodo.cl/products/209184-asus-rog-strix-scar-16",
                image_url="https://media.solotodo.com/media/products/sample_rog.jpg",
                in_stock=True,
                timestamp=now,
            ),
            # Glitch de Monitor (~88% off)
            ProductItem(
                id="solotodo-glitch-mon-002",
                store_id="solotodo",
                store_name="SoloTodo Meta-Retailer",
                title="Monitor Gamer OLED 34\" Alienware AW3423DWF Curvo WQHD 165Hz",
                category="monitores",
                current_price=129990.0,
                normal_price=1099990.0,
                card_price=None,
                url="https://www.solotodo.cl/products/219082-alienware-aw3423dwf",
                image_url="https://media.solotodo.com/media/products/sample_aw.jpg",
                in_stock=True,
                timestamp=now,
            ),
            # Descuento comercial estándar (no glitch)
            ProductItem(
                id="solotodo-normal-003",
                store_id="solotodo",
                store_name="SoloTodo Meta-Retailer",
                title="Notebook Lenovo IdeaPad Slim 3 15IAH8 (Core i5-12450H / 16GB / 512GB SSD)",
                category="notebooks",
                current_price=449990.0,
                normal_price=549990.0,
                card_price=None,
                url="https://www.solotodo.cl/products/182931-lenovo-ideapad-slim-3",
                image_url="https://media.solotodo.com/media/products/sample_lenovo.jpg",
                in_stock=True,
                timestamp=now,
            ),
            # Caso trampa de accesorio (debe ser descartado por el filtro semántico)
            ProductItem(
                id="solotodo-trap-case-004",
                store_id="solotodo",
                store_name="SoloTodo Meta-Retailer",
                title="Funda de silicona reforzada para ASUS ROG Strix SCAR 16",
                category="accesorios",
                current_price=14990.0,
                normal_price=149990.0,
                card_price=None,
                url="https://www.solotodo.cl/products/298412-funda-silicona-rog",
                image_url="https://media.solotodo.com/media/products/sample_case.jpg",
                in_stock=True,
                timestamp=now,
            ),
        ]
