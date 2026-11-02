"""
Servicio de Referencia de Precios Inter-Tiendas (Cross-Retailer Market Benchmark).
Compara productos contra la mediana del mercado (mediante SoloTodo y catálogo cruzado
por código de modelo estricto) para validar si una oferta es una anomalía genuina
frente a los demás retailers chilenos o un falso descuento por inflación de precio de lista.
"""

from __future__ import annotations

import logging
import statistics
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from glitchhunter_ai.core.model_extractor import extract_model_code
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.scrapers.stealth_client import default_stealth_client

logger = logging.getLogger("glitchhunter.core.market_reference")

# Mapeo de tiendas conocidas para exclusión estricta
STORE_DOMAINS: Dict[str, List[str]] = {
    "falabella": ["falabella.com", "falabella.cl", "9"],
    "ripley": ["ripley.cl", "simple.ripley.cl", "18"],
    "paris": ["paris.cl", "11"],
    "lider": ["lider.cl", "walmart", "43"],
    "mercadolibre": ["mercadolibre.cl", "mercadolibre", "260"],
    "pcfactory": ["pcfactory.cl", "12"],
}


@dataclass
class MarketReferenceResult:
    """Resultado del benchmark de precios inter-tiendas."""
    median_price: float
    median_normal_price: Optional[float]
    stores_count: int
    match_method: str  # "solotodo" | "model_code"
    matched_model: str
    market_discount_pct: float
    is_market_anomaly: bool  # True si el precio actual es >= 40% menor a la mediana del mercado
    is_fake_inflation: bool  # True si el precio normal de la tienda es > 1.8x la mediana
    sample_stores: List[str]


class MarketReferenceService:
    """
    Servicio de consulta y cálculo de mediana de precios entre retailers competidores.
    Excluye estrictamente la tienda del producto evaluado y exige un mínimo de 2 tiendas
    distintas con stock disponible para conformar una referencia válida.
    """

    def __init__(self, timeout: float = 2.5, cache_ttl_hours: float = 8.0):
        self.timeout = timeout
        self.cache_ttl_hours = cache_ttl_hours
        self.client = default_stealth_client

    def get_market_reference(
        self,
        product: ProductItem,
        exclude_store_id: str,
        db: Optional[Any] = None,
    ) -> Optional[MarketReferenceResult]:
        """
        Obtiene la referencia de mercado para un producto candidato.
        Si falla por timeout, no hay coincidencias exactas o hay menos de 2 tiendas,
        retorna None de forma limpia y transparente (no bloquea el pipeline).
        """
        # 1. Extraer código de modelo con regex de alta precisión
        model_code = extract_model_code(product.title)
        if not model_code:
            logger.debug("No se pudo extraer código de modelo para: %s", product.title)
            return None

        cache_key = f"mkt_ref:{model_code.lower()}"

        # 2. Consultar caché local de base de datos (TTL 8 horas)
        if db:
            cached = db.get_cached_market_reference(cache_key)
            if cached:
                logger.debug("Caché hit para referencia de mercado [%s]", model_code)
                return self._build_result(product, cached, model_code)

        # 3. Consultar SoloTodo API con timeout acotado
        t0 = time.perf_counter()
        solotodo_data = self._query_solotodo(model_code, exclude_store_id)
        elapsed = (time.perf_counter() - t0) * 1000.0

        if solotodo_data and solotodo_data.get("stores_count", 0) >= 2:
            solotodo_data["match_method"] = "solotodo"
            solotodo_data["matched_model"] = model_code
            if db:
                db.save_cached_market_reference(
                    cache_key=cache_key,
                    median_price=solotodo_data["median_price"],
                    median_normal_price=solotodo_data.get("median_normal_price"),
                    stores_count=solotodo_data["stores_count"],
                    match_method="solotodo",
                    matched_model=model_code,
                    ttl_hours=self.cache_ttl_hours,
                )
            logger.info(
                "🌐 [SoloTodo Benchmark] %s: Mediana $%s CLP (%d tiendas, %.0f ms)",
                model_code, f"{solotodo_data['median_price']:,.0f}".replace(",", "."),
                solotodo_data["stores_count"], elapsed
            )
            return self._build_result(product, solotodo_data, model_code)

        # 4. Fallback a catálogo local cruzado de las otras tiendas
        if db:
            local_data = db.find_local_cross_store_reference(model_code, exclude_store_id)
            if local_data and local_data.get("stores_count", 0) >= 2:
                local_data["match_method"] = "model_code"
                local_data["matched_model"] = model_code
                db.save_cached_market_reference(
                    cache_key=cache_key,
                    median_price=local_data["median_price"],
                    median_normal_price=local_data.get("median_normal_price"),
                    stores_count=local_data["stores_count"],
                    match_method="model_code",
                    matched_model=model_code,
                    ttl_hours=self.cache_ttl_hours,
                )
                logger.info(
                    "🌐 [Local Model Benchmark] %s: Mediana $%s CLP (%d tiendas)",
                    model_code, f"{local_data['median_price']:,.0f}".replace(",", "."),
                    local_data["stores_count"]
                )
                return self._build_result(product, local_data, model_code)

        return None

    def _query_solotodo(self, model_code: str, exclude_store_id: str) -> Optional[Dict[str, Any]]:
        """Consulta la API pública de SoloTodo buscando el producto y sus entidades por tienda."""
        try:
            q = urllib.parse.quote(model_code)
            search_url = f"https://publicapi.solotodo.com/products/browse/?search={q}&page_size=3"
            resp = self.client.get(search_url, timeout=self.timeout)
            if resp.status_code != 200:
                return None

            data = resp.json()
            results = data.get("results", [])
            if not results:
                return None

            # Encontrar el producto más representativo
            prod_id = None
            for r in results:
                entries = r.get("product_entries", [])
                if entries:
                    prod = entries[0].get("product", {})
                    p_name = prod.get("name", "").lower()
                    if model_code.lower() in p_name:
                        prod_id = prod.get("id")
                        break
            if not prod_id:
                # Tomar el primer resultado si no hay match exacto en nombre
                prod_id = results[0].get("product_entries", [{}])[0].get("product", {}).get("id")

            if not prod_id:
                return None

            # Consultar tiendas asociadas con stock
            ent_url = f"https://publicapi.solotodo.com/products/{prod_id}/entities/"
            ent_resp = self.client.get(ent_url, timeout=self.timeout)
            if ent_resp.status_code != 200:
                return None

            entities = ent_resp.json()
            store_prices: Dict[str, Dict[str, float]] = {}
            exclude_keywords = STORE_DOMAINS.get(exclude_store_id.lower(), [exclude_store_id.lower()])

            for e in entities:
                reg = e.get("active_registry")
                if not reg or not reg.get("is_available"):
                    continue

                sid = str(e.get("store_id") or e.get("store") or "")
                ext_url = (e.get("external_url") or "").lower()

                # Regla 1: Excluir estrictamente la propia tienda que se está evaluando
                is_excluded = any(kw in ext_url or kw == sid for kw in exclude_keywords)
                if is_excluded:
                    continue

                offer_p = float(reg.get("offer_price", 0.0))
                norm_p = float(reg.get("normal_price") or offer_p)

                if offer_p > 0:
                    if sid not in store_prices or offer_p < store_prices[sid]["offer"]:
                        store_prices[sid] = {"offer": offer_p, "normal": norm_p}

            # Regla 2: Exigir al menos 2 tiendas distintas con stock
            if len(store_prices) < 2:
                return None

            offers = [v["offer"] for v in store_prices.values()]
            normals = [v["normal"] for v in store_prices.values()]

            return {
                "median_price": statistics.median(offers),
                "median_normal_price": statistics.median(normals) if normals else None,
                "stores_count": len(store_prices),
                "sample_stores": list(store_prices.keys()),
            }
        except Exception as exc:
            logger.debug("Error consultando SoloTodo API para %s: %s", model_code, exc)
            return None

    def _build_result(
        self,
        product: ProductItem,
        data: Dict[str, Any],
        model_code: str,
    ) -> MarketReferenceResult:
        """Calcula las 2 señales analíticas de mercado sobre los datos obtenidos."""
        current_price = float(product.best_price)
        normal_price = float(product.normal_price or current_price)
        median_price = float(data["median_price"])
        median_normal = float(data.get("median_normal_price") or median_price)
        stores_count = int(data.get("stores_count", 0))
        match_method = str(data.get("match_method", "model_code"))

        # Señal 1: Descuento respecto a la mediana del mercado
        market_discount_pct = round(((median_price - current_price) / median_price) * 100.0, 1) if median_price > 0 else 0.0
        is_market_anomaly = market_discount_pct >= 40.0

        # Señal 2: Verificación de inflación artificial inter-tiendas
        # Si el precio normal de la tienda es > 1.8x la mediana del mercado mientras el precio actual no es ganga
        is_fake_inflation = (normal_price > 1.8 * median_price) and (market_discount_pct < 30.0)

        return MarketReferenceResult(
            median_price=median_price,
            median_normal_price=median_normal,
            stores_count=stores_count,
            match_method=match_method,
            matched_model=model_code,
            market_discount_pct=market_discount_pct,
            is_market_anomaly=is_market_anomaly,
            is_fake_inflation=is_fake_inflation,
            sample_stores=data.get("sample_stores", []),
        )


# Instancia singleton predeterminada
default_market_reference = MarketReferenceService()
