"""
GlitchHunter AI - Pipeline Orquestador Multietapa
Coordina la extracción de datos desde los 6 scrapers de retail chileno,
el cálculo de anomalías matemáticas (descuentos masivos y Missing Zero Bug),
la verificación de inflación artificial (precios falsos de CyberDay)
y el filtro semántico NLP estricto para erradicar el 'Carcasa Trap'.
"""

from collections import OrderedDict
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from glitchhunter_ai.ai_detector.inflation_verifier import InflationVerifier
from glitchhunter_ai.ai_detector.math_detector import MathAnomalyDetector
from glitchhunter_ai.ai_detector.semantic_filter import SemanticGlitchFilter
from glitchhunter_ai.core.cart_sniper import generate_direct_add_to_cart_url
from glitchhunter_ai.core.categories import ACCESSORY_CATEGORY, NON_TECH_CATEGORIES, classify_category
from glitchhunter_ai.core.price_signals import BULK_MIN_PRODUCTS, detect_bulk_prices, risk_tier
from glitchhunter_ai.scrapers.live_feed import KNASTA_DEAL_STATES
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.market_reference import default_market_reference
from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    ProductItem,
    SemanticClassificationResult,
    VerificationLevel,
)
from glitchhunter_ai.core.store_health import global_store_health
from glitchhunter_ai.notifications import DiscordAlertDispatcher, NtfyAlertDispatcher, TelegramAlertDispatcher
from glitchhunter_ai.scrapers import get_all_scrapers
from glitchhunter_ai.scrapers.direct_reader import DIRECT_STORES, STORE_NAMES, default_direct_reader

logger = logging.getLogger("GlitchHunter.Pipeline")

# Productos que no son nuevos: su "precio normal" suele ser el de uno nuevo y el descuento es engañoso
REFURBISHED_RE = re.compile(
    r"\b(?:reacondicionad[oa]s?|refurbished|renovad[oa]|usad[oa]|seminuev[oa]|open\s?box|"
    r"segunda\s+selecci[oó]n|exhibici[oó]n|outlet|caja\s+abierta|da[ñn]ad[oa]s?)\b",
    re.IGNORECASE,
)
# Servicios que se venden junto al producto (garantías, instalación, gift cards): su "precio" depende
# del producto al que se asocian y su historial salta sin que haya rebaja real (Electrolux: "Garantía
# Extendida" de $10.184 marcada como caída del 78%). No son ofertas ni errores de precio.
SERVICE_RE = re.compile(
    r"\b(?:garant[ií]a\s+extendida|extensi[oó]n\s+de\s+garant[ií]a|servicio\s+de\s+(?:instalaci[oó]n|armado)|"
    r"servicio\s+t[eé]cnico|gift\s?card|tarjeta\s+de\s+regalo)\b",
    re.IGNORECASE,
)
# Títulos de accesorios que nombran al equipo ("Funda para iPhone 15", "compatible con MacBook")
PRIORITY_ACCESSORY_RE = re.compile(r"\b(?:para|compatible|compatibles|for|con\s+cable\s+para)\b")

GENERIC_BRAND_NAMES = {"generico", "genérico", "oem", "sin marca", "generic", "importado"}
KNOWN_TECH_BRANDS_RE = re.compile(
    r"\b(apple|iphone|ipad|macbook|airpods|samsung|galaxy|garmin|huawei|xiaomi|redmi|poco|motorola|honor|oppo|vivo|realme|"
    r"sony|playstation|ps5|ps4|nintendo|switch|xbox|lg|tcl|hisense|caixun|philips|panasonic|aoc|benq|viewsonic|gigabyte|"
    r"msi|asus|rog|tuf|acer|lenovo|hp|omen|victus|dell|alienware|razer|logitech|hyperx|corsair|jbl|bose|sennheiser|"
    r"harman|anker|soundcore|marshall|edifier|amazfit|fitbit|polar|suunto|casio|gopro|dji|canon|nikon|epson|brother|"
    r"sindelen|thomas|oster|ursus|trotter|somela|midea|mademsa|fensa|electrolux|whirlpool|bosch|teka|fdv|easyways|"
    r"ninja|instant\s+pot|delonghi|nespresso|krups|moulinex|braun|remington|babyliss|siegen|gama|karcher|black\+?decker|"
    r"makita|dewalt|stanley|einhell|bauker|levis|nike|adidas|puma|under\s+armour|reebok|new\s+balance|columbia|lippi)\b",
    re.IGNORECASE,
)
GENERIC_TECH_TRAP_RE = re.compile(
    r"\b(hk\d{2,3}|gt08|s37|i58\d+|iw\d+|5s01|km88|sk40|stn-?28|t800|t900|w26|w68|y68|d20)\b"
    r"|smartwatch\s+reloj\s+inteligente"
    r"|reloj\s+inteligente\s+(?:llamadas|bluetooth|smartwatch|mujer|hombre|deportivo|control|conectividad)"
    r"|smartwatch\s+(?:mujer|hombre|control\s+de\s+salud|pantalla\s+hd|doble\s+correa)"
    r"|aud[ií]fonos.*(?:cat\s+gato|cat\s+light|orejas\s+de\s+gato|modelo\s+m1\s+cat)"
    r"|micr[oó]fono\s+inal[aá]mbrico\s+solapa\s+universal"
    r"|calculadora\s+solar\s+de\s+escritorio",
    re.IGNORECASE,
)
console = Console()


class GlitchHuntingPipeline:
    """
    Motor central que orquesta la detección colaborativa de bugs de precios.
    """

    def __init__(
        self,
        db: Optional[PriceDatabase] = None,
        telegram_dispatcher: Optional[TelegramAlertDispatcher] = None,
        ntfy_dispatcher: Optional[NtfyAlertDispatcher] = None,
        discord_dispatcher: Optional[DiscordAlertDispatcher] = None,
        on_glitch_callback: Optional[Callable[[GlitchAlert], Any]] = None,
        direct_reader: Optional[Any] = None,
        confirm_with_store: Optional[bool] = None,
        forward_only: bool = False,
    ):
        from glitchhunter_ai.config import DIRECT_CONFIRM_ENABLED

        self.db = db or PriceDatabase()
        self.direct_reader = direct_reader or default_direct_reader
        self.confirm_with_store = DIRECT_CONFIRM_ENABLED if confirm_with_store is None else confirm_with_store
        self.math_detector = MathAnomalyDetector()
        self.inflation_verifier = InflationVerifier()
        self.semantic_filter = SemanticGlitchFilter()
        self.telegram_dispatcher = telegram_dispatcher or TelegramAlertDispatcher()
        self.ntfy_dispatcher = ntfy_dispatcher or NtfyAlertDispatcher()
        self.discord_dispatcher = discord_dispatcher or DiscordAlertDispatcher()
        self.scrapers = get_all_scrapers()
        self.on_glitch_callback = on_glitch_callback
        from glitchhunter_ai.core.catalog_rotator import CatalogRotator
        self.catalog_rotator = CatalogRotator(state_db=self.db)
        self._orchestrator_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        # Productos con el mismo precio que otros de su tienda en el escaneo en curso (carga masiva)
        self._bulk_price_counts: Dict[str, int] = {}
        self.last_direct_scan_skipped = False   # True si el último escaneo directo se omitió por otro en curso
        self.is_scanning = False
        self._rejected_deals: OrderedDict = OrderedDict()   # (product_id, precio) rechazados (acotado a 5000)
        self._rejected_deals_max = 5000
        self.forwarder = None   # core/forwarder.Forwarder: envía cada lectura a otro radar (FORWARD_TO_URL)
        self.forward_only = bool(forward_only)
        self._forward_seen_prices: Dict[str, float] = {}
        self._store_inflation_cache: Dict[str, Dict[str, Any]] = {}
        self._store_inflation_cache_ts: float = 0.0

    def _get_store_inflation_map(self, ttl_seconds: float = 300.0) -> Dict[str, Dict[str, Any]]:
        """Perfil empírico cacheado de tiendas con inflación crónica de precios de lista."""
        now = time.monotonic()
        if self._store_inflation_cache and (now - self._store_inflation_cache_ts) < ttl_seconds:
            return self._store_inflation_cache
        getter = getattr(self.db, "get_store_inflation_stats", None)
        if callable(getter):
            try:
                data = getter()
                if isinstance(data, dict):
                    self._store_inflation_cache = data
                    self._store_inflation_cache_ts = now
            except Exception as exc:
                logger.debug(f"No se pudo calcular perfil de inflación por tienda: {exc}")
        return self._store_inflation_cache

    def _inflated_store_ids(self) -> List[str]:
        return [
            sid for sid, info in self._get_store_inflation_map().items()
            if isinstance(info, dict) and info.get("is_chronic_inflator")
        ]

    @staticmethod
    def is_generic_marketplace_trap(
        title: str,
        brand: Optional[str] = None,
        seller: Optional[str] = None,
    ) -> bool:
        """Detecta clones genéricos 3P inflados (HK80/GT08/Cat Ear) salvo que tengan marca real reconocida."""
        title_s = title or ""
        brand_s = (brand or "").strip()
        if KNOWN_TECH_BRANDS_RE.search(title_s) or (brand_s and KNOWN_TECH_BRANDS_RE.search(brand_s)):
            return False
        if brand_s.lower() in GENERIC_BRAND_NAMES:
            return True
        return bool(GENERIC_TECH_TRAP_RE.search(title_s))

    def process_product(
        self,
        product: ProductItem,
        send_notifications: bool = True,
        _already_upserted: bool = False,
        _baseline_price: Optional[float] = None,
    ) -> Optional[GlitchAlert]:
        """
        Ejecuta el protocolo de análisis para un solo producto.
        Verifica stock, gestiona transiciones de estado (correcciones y caídas)
        y detecta anomalías matemáticas y de precios.
        """
        # Fase 0: Verificación de Stock (Productos sin stock no generan alertas)
        if not product.in_stock:
            if self.db.get_deal_status(product.id) == "ACTIVE":
                self.db.mark_deal_out_of_stock(product.id)
            logger.debug(f"📦 [Sin Stock Omitido] {product.title} ({product.id}) no tiene stock disponible.")
            return None

        # Fase 1: Consulta de precio histórico base previo (30d) y persistencia
        baseline_price = _baseline_price if _baseline_price is not None else self.db.get_baseline_price(product.id, days=30)
        if not _already_upserted:
            self.db.upsert_product(product)

        # Fase 2: Detector Matemático de Anomalías (Porcentaje y Missing Zero Bug)
        math_eval = self.math_detector.evaluate(product, baseline_price=baseline_price)
        is_candidate = (
            math_eval.get("is_glitch_candidate")
            or math_eval.get("is_math_glitch")
            or math_eval.get("is_suspicious")
        )

        if not is_candidate:
            # Si el producto estaba ACTIVO en el radar y el precio ya no es glitch,
            # la tienda corrigió el precio (volvió a precio normal / liquidación común)
            if self.db.get_deal_status(product.id) == "ACTIVE":
                self.db.mark_deal_corrected(product.id, product.best_price)
                logger.info(
                    f"🔄 [Oferta Corregida] {product.title} ({product.id}) subió a ${product.best_price:,.0f} CLP. "
                    f"Estado en radar transicionado a CORRECTED."
                )
                if self.on_glitch_callback:
                    try:
                        self.on_glitch_callback({
                            "type": "deal_corrected",
                            "product_id": product.id,
                            "new_price": product.best_price,
                        })
                    except Exception:
                        pass
            return None

        # Fase 3: Verificación de Inflación Artificial (Fake CyberDay Deals)
        is_fake_inflation, real_discount, inflation_explanation = (
            self.inflation_verifier.verify_inflation(product, baseline_price=baseline_price)
        )
        if is_fake_inflation and real_discount < self.inflation_verifier.min_glitch_discount:
            logger.debug(
                f"🚫 [Inflación Falsa Rechazada] {product.title} "
                f"({product.store_name}) - {inflation_explanation}"
            )
            return None

        # Fase 4: Filtro Semántico NLP Anti-Carcasas
        semantic_eval: SemanticClassificationResult = self.semantic_filter.classify(
            title=product.title, category=product.category
        )

        # Si el clasificador semántico detecta accesorio y NO dispositivo principal
        if semantic_eval.is_accessory and not semantic_eval.is_main_device:
            logger.debug(
                f"🛡️ [Trap Descartado] {product.title} ({product.store_name}) "
                f"clasificado como accesorio ({semantic_eval.detected_item_type}) a precio comercial."
            )
            return None

        # Reacondicionados / usados / open box: la tienda los compara contra el precio de uno nuevo
        if REFURBISHED_RE.search(product.title):
            logger.debug(f"♻️ [Reacondicionado Omitido] {product.title} ({product.store_name}).")
            return None
        if SERVICE_RE.search(product.title):
            logger.debug(f"🧾 [Servicio Omitido] {product.title} ({product.store_name}).")
            return None
        if self.is_generic_marketplace_trap(
            product.title, getattr(product, "brand", None), getattr(product, "seller", None)
        ):
            if not (baseline_price and baseline_price > 0 and product.best_price <= baseline_price * 0.6):
                logger.debug(f"🛡️ [Marketplace Genérico Omitido] {product.title} ({product.store_name}).")
                return None

        # ⏱️ Confirmación en tiempo real: los precios del agregador llegan con ~40 min de retraso.
        # Antes de alertar se relee el producto directamente en la tienda (Falabella / Ripley).
        from glitchhunter_ai.config import store_enabled

        if (
            self.confirm_with_store
            and product.store_id in DIRECT_STORES
            and store_enabled(product.store_id)
            and product.source in ("solotodo", "knasta")
        ):
            fresh = self.direct_reader.fetch_product(product.store_id, str(product.url))
            if fresh is None:
                logger.debug(f"[Confirmación] {product.store_id} no respondió; se usa el precio del agregador para {product.id}.")
            else:
                if not fresh.in_stock:
                    logger.info(f"🔁 [Confirmación] {product.title} ya no tiene stock en {product.store_name}. Alerta omitida.")
                    return None
                product = product.model_copy(update={
                    "current_price": fresh.current_price,
                    "normal_price": fresh.normal_price,
                    "card_price": fresh.card_price,
                    "in_stock": fresh.in_stock,
                    "source": "direct",
                    "timestamp": fresh.timestamp,
                })
                self.db.upsert_product(product)
                math_eval = self.math_detector.evaluate(product, baseline_price=baseline_price)
                if not (
                    math_eval.get("is_glitch_candidate")
                    or math_eval.get("is_math_glitch")
                    or math_eval.get("is_suspicious")
                ):
                    logger.info(
                        f"🔁 [Confirmación] {product.title}: la tienda ya muestra ${product.best_price:,.0f} "
                        f"(el agregador tenía un precio antiguo). Alerta omitida."
                    )
                    return None

        # 🌐 Consulta de Referencia de Mercado Inter-Tiendas
        # Se consulta solo tras los filtros locales (inflación propia y semántico) para no gastar
        # llamadas externas en accesorios o falsos descuentos ya descartados.
        # Productos de prueba/simulación (test-*) omiten consulta externa a SoloTodo
        is_simulated_test = (
            product.id.startswith("test-")
            or product.store_id.startswith("test_")
            or getattr(product, "is_mock", False)
        )
        if is_simulated_test:
            mkt_ref = None
        else:
            mkt_ref = default_market_reference.get_market_reference(
                product=product,
                exclude_store_id=product.store_id,
                db=self.db,
            )

        # Señal 2: Inflación inter-tiendas. No descarta la alerta (un match erróneo no debe
        # ocultar un glitch real): la marca, penaliza su confianza y omite el push al celular.
        market_inflation = bool(mkt_ref and mkt_ref.is_fake_inflation)
        if market_inflation:
            is_fake_inflation = True
            logger.debug(
                f"⚠️ [Inflación Cruzada] {product.title}: normal ${product.normal_price:,.0f} "
                f"> 1.8x mediana mercado ${mkt_ref.median_price:,.0f}. Alerta sin push."
            )

        # ¿Hay evidencia INDEPENDIENTE de la tienda de que el precio realmente se desplomó?
        # El precio tachado lo pone la tienda: perfumes, ropa o porcelanato muestran "-70%" todo el año.
        best_now = product.best_price
        bulk_count = self._bulk_price_counts.get(product.id, 0)
        inflation_map = self._get_store_inflation_map()
        store_inflated = bool((inflation_map.get(product.store_id.lower()) or {}).get("is_chronic_inflator"))
        hist_stats: Dict[str, Any] = {}
        hist_getter = getattr(self.db, "get_price_history_summary", None)
        if callable(hist_getter):
            try:
                raw_hist = hist_getter(product.id, days=90)
                if isinstance(raw_hist, dict):
                    hist_stats = raw_hist
            except Exception as exc:
                logger.debug(f"Error leyendo resumen histórico para {product.id}: {exc}")
        pre_event_spike = bool(hist_stats.get("pre_event_spike"))
        modified_z = InflationVerifier.modified_z_score(best_now, hist_stats.get("prices_90d"))

        has_hist_drop = bool(
            (baseline_price and baseline_price > 0 and best_now <= baseline_price * 0.5)
            or (
                hist_stats.get("median_90d")
                and best_now <= float(hist_stats["median_90d"]) * 0.5
                and float(hist_stats.get("days_span") or 0) >= 0.5
            )
        )
        has_mkt_drop = bool(mkt_ref and not market_inflation and mkt_ref.market_discount_pct >= 50.0)
        has_knasta_drop = bool(
            getattr(product, "knasta_has_habitual", False)
            and (
                not getattr(product, "knasta_state", None)
                or product.knasta_state in KNASTA_DEAL_STATES
            )
        )
        has_bulk_drop = bool(bulk_count >= BULK_MIN_PRODUCTS and not store_inflated)

        # Tiendas con inflación crónica de MSRP no pueden autoverificar alertas sin historial ni mercado
        if store_inflated and not (has_hist_drop or has_mkt_drop or has_knasta_drop) and not is_simulated_test:
            logger.debug(
                f"🚫 [Tienda MSRP Inflado] {product.title} ({product.store_name}) omitido sin respaldo histórico/mercado."
            )
            return None

        # Determinación de severidad
        severity_val = math_eval.get("severity")
        if isinstance(severity_val, GlitchSeverity):
            severity = severity_val
        elif isinstance(severity_val, str) and severity_val in GlitchSeverity.__members__:
            severity = GlitchSeverity[severity_val]
        else:
            discount = math_eval.get("discount_percentage", 0.0)
            if discount >= 85.0 or math_eval.get("is_missing_zero"):
                severity = GlitchSeverity.CRITICO
            elif discount >= 70.0:
                severity = GlitchSeverity.ALTO
            else:
                severity = GlitchSeverity.MODERADO

        discount_pct = float(math_eval.get("discount_percentage", 0.0))
        savings_clp = float(math_eval.get("absolute_savings_clp", 0.0))
        missing_zero = bool(math_eval.get("is_missing_zero", False))
        # Evitar falsos "Missing Zero" en ropa/hogar baratos o si historial/mercado/pre-evento contradice
        nontech_cheap_trap = bool(
            classify_category(product.title) in NON_TECH_CATEGORIES
            and product.category in NON_TECH_CATEGORIES
            and best_now <= 25000.0
            and product.normal_price < 200000.0
            and not has_hist_drop
            and not has_mkt_drop
            and not has_knasta_drop
        )
        if market_inflation or pre_event_spike or nontech_cheap_trap or (store_inflated and not (has_hist_drop or has_mkt_drop)):
            missing_zero = False

        verified_drop = bool(
            has_hist_drop
            or has_mkt_drop
            or has_knasta_drop
            or has_bulk_drop
            or missing_zero
        )
        confidence = float(math_eval.get("confidence_score", 95.0))
        if pre_event_spike:
            is_fake_inflation = True
            confidence = max(25.0, confidence - 20.0)

        if has_hist_drop:
            span_days = int(round(max(1.0, float(hist_stats.get("days_span") or 30.0))))
            evidence_source = "historial_30d"
            evidence_label = f"Bajó de su precio real en {span_days}d"
        elif has_mkt_drop:
            evidence_source = "mercado"
            evidence_label = f"{mkt_ref.market_discount_pct:.0f}% más barato que {mkt_ref.stores_count} tiendas"
        elif has_knasta_drop:
            evidence_source = "knasta_habitual"
            evidence_label = "Bajo su precio habitual registrado en Knasta"
        elif has_bulk_drop:
            evidence_source = "carga_masiva"
            evidence_label = f"{bulk_count} productos distintos al mismo precio exacto"
        elif missing_zero:
            evidence_source = "missing_zero_tech"
            evidence_label = "Posible error de tipeo (~10x menos)"
        else:
            evidence_source = "solo_precio_lista"
            evidence_label = "Basado solo en precio tachado de la tienda (sin historial suficiente)"

        # Nunca se publica un "error de precio" sin descuento (p. ej. el precio confirmado en la tienda
        # ya no tiene rebaja, o dos productos distintos compartían ID y "saltaba" el precio).
        if discount_pct <= 0.0 or savings_clp <= 0.0:
            logger.debug(f"🚫 [Sin descuento] {product.title} ({product.id}): alerta omitida.")
            return None
        # La caída se mide contra nuestro historial: ese es el "antes" que se muestra en la publicación
        from_history = bool(math_eval.get("reference_is_baseline"))
        if from_history:
            product = product.model_copy(update={"normal_price": float(math_eval.get("reference_price") or product.normal_price)})

        # Modulación de confianza según Señal 1 (mercado)
        if market_inflation:
            confidence = max(20.0, confidence - 25.0)
        elif mkt_ref:
            if mkt_ref.market_discount_pct >= 60.0:
                confidence = min(99.0, confidence + 5.0)
            elif mkt_ref.market_discount_pct <= 15.0:
                confidence = max(40.0, confidence - 10.0)

        # Modulación según Feedback Histórico del Usuario (Punto 5)
        trust_mod = 0.0
        try:
            trust_mod = self.db.get_category_trust_modifier(product.store_id, product.category)
            if trust_mod != 0.0:
                confidence = max(20.0, min(99.0, confidence + trust_mod))
        except Exception as e:
            logger.warning(f"Error al calcular trust modifier para {product.store_id}/{product.category}: {e}")

        # Construcción del veredicto estructurado
        verdict = GlitchVerdict(
            is_glitch=True,
            severity=severity,
            confidence_score=confidence,
            discount_percentage=discount_pct,
            absolute_savings_clp=savings_clp,
            is_missing_zero=missing_zero,
            is_fake_inflation=is_fake_inflation,
            semantic_check=semantic_eval,
            market_median_price=mkt_ref.median_price if mkt_ref else None,
            market_normal_median_price=mkt_ref.median_normal_price if mkt_ref else None,
            market_discount_pct=mkt_ref.market_discount_pct if mkt_ref else None,
            market_stores_count=mkt_ref.stores_count if mkt_ref else 0,
            market_match_method=mkt_ref.match_method if mkt_ref else None,
            market_matched_model=mkt_ref.matched_model if mkt_ref else None,
            user_trust_modifier=trust_mod if trust_mod != 0.0 else None,
            evidence_source=evidence_source,
            evidence_label=evidence_label,
            baseline_30d=hist_stats.get("median_30d") or baseline_price,
            baseline_90d=hist_stats.get("median_90d"),
            min_90d=hist_stats.get("min_90d"),
            history_days=float(hist_stats.get("days_span") or 0.0),
            modified_z_score=modified_z,
            inflation_warning=bool(market_inflation or pre_event_spike or store_inflated),
            risk_tier_label=risk_tier(discount_pct),
            verified_drop=verified_drop,
            reason=(
                f"{'⚠️ Missing Zero Bug (~10x)' if missing_zero else f'Descuento masivo {discount_pct:.1f}%'} "
                f"en {product.store_name} | Ahorro: ${savings_clp:,.0f} CLP".replace(",", ".")
                + (" | 📉 Caída vs su precio habitual (la tienda no muestra rebaja)" if from_history else "")
                + (" | ⚠️ Precio normal inflado vs mercado" if market_inflation else "")
                + (" | 📈 Inflación previa detectada en historial" if pre_event_spike else "")
                + (f" | 📦 Mismo precio en {bulk_count} productos (posible error de carga masiva)" if has_bulk_drop else "")
            ),
        )

        alert_id = f"ALT-{product.store_id.upper()}-{uuid.uuid4().hex[:8].upper()}"

        # 🎯 MODO SNIPER: Generar enlace directo para agregar al carrito en 1 clic
        sniper_url = generate_direct_add_to_cart_url(product.store_id, product.url, product.id)
        product.add_to_cart_url = sniper_url

        # Cadena de Confiabilidad LIVE: Determinar nivel operativo (Punto 4 de Auditoría)
        # El enlace Sniper se construye localmente a partir de patrones de URL y nunca se valida
        # contra la tienda, por lo que no basta para declarar la oferta COMPRABLE.
        if product.in_stock:
            verif_level = VerificationLevel.VERIFICADO
        else:
            verif_level = VerificationLevel.DETECTADO
        verdict.verification_level = verif_level

        # 🧠 MOTOR DE EXCELENCIA: Revisor Continuo y Registro de Estado
        # Consulta si la oferta ya está registrada y notificada previamente.
        # Si ya existe al mismo precio, actualiza su presencia sin reenviar notificaciones al celular.
        is_new_deal, deal_reason = self.db.register_or_update_deal(
            product=product,
            verdict=verdict,
            alert_id=alert_id
        )
        if not is_new_deal:
            logger.debug(
                f"ℹ️ [Revisor Continuo] Oferta {product.id} ya activa ({deal_reason}). "
                f"Preservada en el radar, omitiendo notificación repetida."
            )
            return None

        # ⭐ REVISIÓN DE WATCHLIST: Prioridad especial sin corromper la certeza matemática (Hallazgo E de Auditoría 4)
        is_watchlist = self.db.is_in_watchlist(product.title, price=product.best_price)
        if is_watchlist:
            logger.info("⭐ [WATCHLIST HIT] %s coincide con las palabras clave de seguimiento prioritario!", product.title)

        alert = GlitchAlert(
            alert_id=alert_id,
            product=product,
            verdict=verdict,
            alert_url=str(product.url),
            add_to_cart_url=sniper_url,
            is_watchlist_match=is_watchlist,
            verification_level=verif_level,
            market_median_price=mkt_ref.median_price if mkt_ref else None,
            market_discount_pct=mkt_ref.market_discount_pct if mkt_ref else None,
            market_stores_count=mkt_ref.stores_count if mkt_ref else 0,
            market_match_method=mkt_ref.match_method if mkt_ref else None,
            market_matched_model=mkt_ref.matched_model if mkt_ref else None,
            created_at=datetime.now(timezone.utc),
            telegram_sent=False,
        )

        # Registro en BD SQLite
        try:
            self.db.record_glitch_alert(alert)
        except Exception as e:
            logger.warning(f"No se pudo registrar alerta en BD: {e}")

        # Publicación en el canal de ofertas (una sola vez por producto y precio).
        # Las simulaciones (test-*, "Simular Glitch", demo de watchlist) nunca se publican:
        # el canal guarda las ofertas para siempre y solo debe contener datos reales.
        is_simulated = product.id.startswith("test-") or getattr(product, "is_mock", False)
        try:
            if not is_simulated and verified_drop:
                self.db.publish_feed_offer(
                    kind="glitch",
                    product_id=product.id,
                    store_id=product.store_id,
                    best_price=product.best_price,
                    discount_pct=discount_pct,
                    payload={**alert.model_dump(mode="json"), "bulk_price_count": bulk_count or None},
                )
            elif not is_simulated:
                self._publish_unverified_as_deal(product, discount_pct, savings_clp)
        except Exception as e:
            logger.warning(f"No se pudo publicar el glitch en el canal: {e}")

        # Despacho de notificaciones (si send_notifications está habilitado y no es producto de prueba)
        telegram_ok = False
        ntfy_ok = False
        discord_ok = False
        is_test_product = (
            product.id.startswith("test-")
            or product.store_id.startswith("test_")
            or getattr(product, "is_mock", False)
        )

        push_allowed = bool(send_notifications and not is_test_product and not market_inflation and verified_drop)
        if push_allowed:
            llm_gate = self.semantic_filter.verify_glitch_with_llm(product, evidence_label)
            if llm_gate is not None and not llm_gate.get("approved", True):
                logger.info(
                    f"🤖 [LLM Filtro] Push omitido para {product.title} ({product.store_name}): {llm_gate.get('reason')}"
                )
                push_allowed = False

        if push_allowed:
            # Despacho por Telegram
            try:
                dispatch_ok = self.telegram_dispatcher.send_alert(alert)
                telegram_ok = bool(dispatch_ok)
                if telegram_ok:
                    self.db.mark_telegram_sent(alert.alert_id)
                    alert.telegram_sent = True
            except Exception as e:
                logger.error(f"Error despachando alerta de Telegram para {alert_id}: {e}")

            # Despacho push a celular vía ntfy (con apertura automática de URL al presionar)
            try:
                ntfy_ok = bool(self.ntfy_dispatcher.send_alert(alert))
            except Exception as e:
                logger.error(f"Error despachando notificación push ntfy para {alert_id}: {e}")

            # Despacho enriquecido a Discord Webhook
            try:
                discord_ok = bool(self.discord_dispatcher.send_alert(alert))
            except Exception as e:
                logger.error(f"Error despachando alerta a Discord para {alert_id}: {e}")
        else:
            logger.debug(
                f"ℹ️ [Aislamiento Simulación] Notificaciones push externas omitidas para {alert_id} (send_notifications={send_notifications})"
            )
            # En modo simulación o test, invocar los despachadores en modo simulación para validar payloads sin emitir HTTP
            if getattr(self.telegram_dispatcher, "simulation_mode", False) or getattr(self.telegram_dispatcher, "is_simulation", False):
                try:
                    self.telegram_dispatcher.send_alert(alert)
                except Exception:
                    pass

        # Registro de métrica de salud en matriz
        global_store_health.record_glitch(product.store_id)

        # Confirmación de despacho en el radar tracked_deals
        is_sim = getattr(self.telegram_dispatcher, "is_simulation", False)
        if telegram_ok or ntfy_ok or discord_ok or is_sim:
            self.db.mark_deal_dispatched(product.id)

        # Callback a clientes WebSocket si está conectado
        if self.on_glitch_callback:
            try:
                self.on_glitch_callback(alert)
            except Exception as e:
                logger.warning(f"Error en on_glitch_callback: {e}")

        return alert

    def run_scan(
        self,
        use_live: bool = False,
        store_ids: Optional[List[str]] = None,
        send_notifications: Optional[bool] = None,
        rotate_categories: bool = True,
        scan_watchlist: bool = True,
        wait_for_lock_seconds: float = 0.0,
    ) -> List[GlitchAlert]:
        """
        Ejecuta un ciclo de escaneo completo a través de todos los scrapers seleccionados.
        Integra rotación Round-Robin de categorías, ordenamiento prioritario ('discount_desc' / 'price_asc')
        y escaneo proactivo de la Watchlist del usuario.
        Protegido con un cerrojo atómico para impedir carreras por escaneos concurrentes (Hallazgo F).
        """
        # El escaneo automático espera a que termine un escaneo en curso; un escaneo manual vía API no espera.
        # _orchestrator_lock serializa escaneos macro, mientras _scan_lock solo protege procesamiento y SQLite
        # para no bloquear lecturas continuas durante descargas HTTP lentas.
        orch_acquired = (
            self._orchestrator_lock.acquire(timeout=wait_for_lock_seconds)
            if wait_for_lock_seconds > 0
            else self._orchestrator_lock.acquire(blocking=False)
        )
        if not orch_acquired:
            logger.warning("⚠️ Intento de escaneo concurrente rechazado: ya existe un ciclo de escaneo en ejecución.")
            return []

        scan_acquired = (
            self._scan_lock.acquire(timeout=wait_for_lock_seconds)
            if wait_for_lock_seconds > 0
            else self._scan_lock.acquire(blocking=False)
        )
        if not scan_acquired:
            self._orchestrator_lock.release()
            logger.warning("⚠️ Intento de escaneo concurrente rechazado: cerrojo de base de datos ocupado.")
            return []
        self._scan_lock.release()

        self.is_scanning = True
        all_detected_glitches: List[GlitchAlert] = []

        try:
            if send_notifications is None:
                send_notifications = bool(use_live)

            # Expirar ofertas que no han sido vistas en las últimas 12 horas
            try:
                with self._scan_lock:
                    expired = self.db.expire_stale_deals(max_hours=12)
                if expired > 0:
                    logger.info(f"⏳ [Expiración Automática] {expired} oferta(s) no vistas en >12h transicionadas a EXPIRED.")
            except Exception as e:
                logger.debug(f"Error en expire_stale_deals: {e}")

            from glitchhunter_ai.config import store_enabled
            target_scrapers = [
                s for s in self.scrapers
                if (store_ids is None or s.store_id in store_ids) and store_enabled(s.store_id)
            ]

            # 1. Rotación Round-Robin de Categorías y Estrategia de Ordenamiento
            rotation_batch = None
            if rotate_categories and self.catalog_rotator:
                rotation_batch = self.catalog_rotator.get_next_batch()

            strategy_desc = rotation_batch.strategy.value if rotation_batch else "estandar"
            logger.debug(
                f"🔎 Iniciando ciclo de escaneo en {len(target_scrapers)} tiendas retail "
                f"(live={use_live}, notify={send_notifications}, orden={strategy_desc})..."
            )

            total_scanned = 0
            for scraper in target_scrapers:
                t_start = time.perf_counter()
                fetch_ms = 0.0
                has_error = False
                products = []
                try:
                    kwargs: Dict[str, Any] = {"use_live": use_live}
                    if rotation_batch:
                        import inspect
                        sig = inspect.signature(scraper.fetch_catalog)
                        if "sort_strategy" in sig.parameters:
                            kwargs["sort_strategy"] = rotation_batch.strategy.value
                        if "active_category_ids" in sig.parameters:
                            cat_ids = [cid for c in rotation_batch.categories for cid in c.solotodo_category_ids]
                            kwargs["active_category_ids"] = cat_ids

                    products = scraper.fetch_catalog(**kwargs)
                    fetch_ms = (time.perf_counter() - t_start) * 1000.0
                    total_scanned += len(products)
                    if use_live and products and self.forwarder is not None:
                        self.forwarder.submit(scraper.store_id, products, latency_ms=round(fetch_ms, 1))   # unir paneles (FORWARD_TO_URL)
                    with self._scan_lock:
                        self._bulk_price_counts = {}
                        self._note_bulk_prices(products)
                        for prod in products:
                            alert = self.process_product(prod, send_notifications=send_notifications)
                            if alert:
                                all_detected_glitches.append(alert)
                        self._bulk_price_counts = {}
                except Exception as exc:
                    if fetch_ms == 0.0:
                        fetch_ms = (time.perf_counter() - t_start) * 1000.0
                    has_error = True
                    logger.error(f"Error escaneando {scraper.store_name}: {exc}")
                finally:
                    global_store_health.record_scan(
                        store_id=scraper.store_id,
                        store_name=scraper.store_name,
                        latency_ms=fetch_ms,
                        items_count=len(products),
                        error=has_error,
                    )
                    if use_live and global_store_health.should_alert_empty_store(scraper.store_id):
                        self._notify_store_health_alert(scraper.store_id, scraper.store_name)

            # 2. Escaneo Prioritario de la Watchlist del Usuario
            if scan_watchlist:
                try:
                    wl_alerts = self._run_watchlist_scan_unlocked(
                        use_live=use_live,
                        send_notifications=send_notifications,
                    )
                    if wl_alerts:
                        all_detected_glitches.extend(wl_alerts)
                except Exception as wl_exc:
                    logger.debug("Error en escaneo de watchlist: %s", wl_exc)

            logger.debug(
                f"✅ Ciclo concluido: {total_scanned} productos analizados, "
                f"{len(all_detected_glitches)} glitches confirmados."
            )
            if use_live:
                with self._scan_lock:
                    self.publish_new_deals()
                    self.refresh_feed_statuses()
            return all_detected_glitches
        finally:
            self.is_scanning = False
            self._orchestrator_lock.release()

    run_scan_cycle = run_scan

    def _note_bulk_prices(self, products: List[ProductItem]) -> None:
        """Registra los productos que comparten precio exacto con otros de su tienda (carga masiva)."""
        counts = detect_bulk_prices(products, excluded_stores=self._inflated_store_ids())
        if counts:
            stores = sorted({p.store_id for p in products if p.id in counts})
            logger.info(f"📦 [Carga masiva] {len(counts)} producto(s) con precio repetido en {', '.join(stores)}.")
        self._bulk_price_counts.update(counts)

    def refresh_feed_statuses(self) -> int:
        """Marca como expiradas (o reactiva) las publicaciones del canal según el último precio visto."""
        from glitchhunter_ai.config import FEED_NOT_SEEN_HOURS

        try:
            changed = self.db.refresh_feed_statuses(not_seen_hours=FEED_NOT_SEEN_HOURS)
        except Exception as exc:
            logger.warning(f"No se pudo revisar la vigencia de las publicaciones: {exc}")
            return 0
        if changed:
            logger.info(f"🔄 [Canal] {changed} publicación(es) cambiaron de vigencia.")
            if self.on_glitch_callback:
                try:
                    self.on_glitch_callback({"type": "feed_status", "changes": changed})
                except Exception:
                    pass
        try:
            last_prune_raw = self.db.get_state("last_history_prune_epoch")
            now_epoch = time.time()
            if not last_prune_raw or (now_epoch - float(last_prune_raw)) >= 43200.0:
                pruned = self.db.prune_old_price_history(keep_days=120, batch_size=25000)
                self.db.set_state("last_history_prune_epoch", str(int(now_epoch)))
                if pruned:
                    logger.info(f"🧹 [Mantenimiento BD] Podadas {pruned} muestra(s) de historial >120 días.")
        except Exception as exc:
            logger.debug(f"Poda periódica de price_history omitida: {exc}")
        return changed

    def withdraw_false_offers(self) -> Dict[str, int]:
        """
        Revisa el canal con las reglas actuales y retira lo que nunca fue una rebaja real (servicios,
        liquidaciones de precio único publicadas como error de carga). Mantenimiento: `run.py retirar-falsos`.
        """
        from glitchhunter_ai.core.price_signals import BULK_EXTREME_DISCOUNT, _is_retail_price_point

        services, outlets, generics = [], [], []
        for post in self.db.get_feed(limit=1_000_000):
            payload = post.get("payload") or {}
            product = payload.get("product") or {}
            title = str(product.get("title") or payload.get("title") or "")
            if SERVICE_RE.search(title):
                services.append(post["id"])
                continue
            if self.is_generic_marketplace_trap(
                title,
                product.get("brand") or payload.get("brand"),
                product.get("seller") or payload.get("seller"),
            ):
                generics.append(post["id"])
                continue
            verdict = payload.get("verdict") or {}
            reason = str(verdict.get("reason") or "")
            if (
                post["kind"] == "glitch"
                and payload.get("bulk_price_count")
                and not verdict.get("is_missing_zero")
                and "habitual" not in reason
                and float(post.get("discount_pct") or 0) < BULK_EXTREME_DISCOUNT
                and _is_retail_price_point(int(round(float(post.get("best_price") or 0))))
            ):
                outlets.append(post["id"])
        self.db.withdraw_feed_offers(services + outlets + generics)
        res = {"servicios": len(services), "liquidaciones": len(outlets)}
        if generics:
            res["genericos"] = len(generics)
        return res

    def _publish_unverified_as_deal(self, product: ProductItem, discount_pct: float, savings_clp: float) -> None:
        """Publica como 'Oferta' un descuento ≥65% sin evidencia independiente (si pasa el filtro de calidad)."""
        row = {
            "product_id": product.id,
            "store_id": product.store_id,
            "title": product.title,
            "brand": getattr(product, "brand", None),
            "seller": getattr(product, "seller", None),
            "category": classify_category(product.title),
            "url": str(product.url),
            "image_url": product.image_url,
            "current_price": product.current_price,
            "card_price": product.card_price,
            "normal_price": product.normal_price,
            "discount_pct": round(discount_pct, 1),
            "savings_clp": round(savings_clp, 0),
        }
        if self._is_publishable_deal(row, product.best_price):
            self.db.publish_feed_offer(
                kind="deal",
                product_id=product.id,
                store_id=product.store_id,
                best_price=product.best_price,
                discount_pct=discount_pct,
                payload=row,
            )

    def _is_publishable_deal(self, row: Dict[str, Any], best_price: float) -> bool:
        """
        Filtro de calidad de las ofertas comerciales antes de publicarlas en el canal:
        - solo simulaciones no, ni productos reacondicionados/usados/open box (su "precio normal" es el de uno nuevo);
        - categoría del radar (no ropa, muebles, etc.) y no accesorios;
        - ahorro mínimo en pesos (un -50% sobre $2.000 no es una oferta relevante).
        """
        from glitchhunter_ai.config import DEAL_GLITCH_ONLY_STORES, DEAL_INCLUDE_NON_TECH, DEAL_MIN_SAVINGS_CLP

        product_id = str(row.get("product_id") or "")
        title = str(row.get("title") or "")
        if product_id.startswith("test-") or "[TEST-" in title:
            return False
        if str(row.get("store_id") or "").lower() in DEAL_GLITCH_ONLY_STORES:
            return False   # tiendas de ropa: solo errores de precio verificados
        store_key = str(row.get("store_id") or "").lower()
        baseline_getter = getattr(self.db, "get_baseline_price", None)
        if (self._get_store_inflation_map().get(store_key) or {}).get("is_chronic_inflator"):
            # En tiendas con inflación crónica de MSRP exigimos caída real contra historial (>=25%)
            baseline = baseline_getter(product_id, days=30) if callable(baseline_getter) and product_id else None
            if not (baseline and baseline > 0 and best_price <= baseline * 0.75):
                return False
        if REFURBISHED_RE.search(title):
            return False
        if self.is_generic_marketplace_trap(title, row.get("brand"), row.get("seller")):
            baseline = baseline_getter(product_id, days=30) if callable(baseline_getter) and product_id else None
            if not (baseline and baseline > 0 and best_price <= baseline * 0.75):
                return False
        category = classify_category(title)
        if category == ACCESSORY_CATEGORY:
            return False
        if category in NON_TECH_CATEGORIES and not DEAL_INCLUDE_NON_TECH:
            return False
        semantic = self.semantic_filter.classify(title=title, category=row.get("category"))
        if semantic.is_accessory and not semantic.is_main_device:
            return False
        normal = float(row.get("normal_price") or 0.0)
        return normal - best_price >= DEAL_MIN_SAVINGS_CLP

    def _priority_terms(self) -> List[str]:
        """Términos con umbral de publicación menor: los de configuración (Apple) y la watchlist."""
        from glitchhunter_ai.config import DEAL_PRIORITY_KEYWORDS
        terms = list(DEAL_PRIORITY_KEYWORDS)
        try:
            terms += [str(k).strip().lower() for k in self.db.get_watchlist_keywords()]
        except Exception:
            pass
        return [t for t in dict.fromkeys(terms) if len(t) >= 3]

    @staticmethod
    def _priority_match(title: str, terms: List[str]) -> Optional[str]:
        """
        Término que nombra al producto mismo (no a un accesorio "para iPhone" / "compatible con MacBook").
        Retorna el término o None.
        """
        low = title.lower()
        if PRIORITY_ACCESSORY_RE.search(low):
            return None
        for term in terms:
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z])", low):
                return term
        return None

    def publish_new_deals(self, min_discount: float = 40.0, max_discount: float = 101.0) -> int:
        """
        Publica en el canal las ofertas (40% o más) que aún no se habían publicado, o que ahora están
        más baratas que en su última publicación. Las ya publicadas no se repiten. Incluye los descuentos
        de 65% o más que no llegaron como error de precio (por ejemplo, ahorros bajo $30.000).
        Solo pasan las que superan el filtro de calidad (ver _is_publishable_deal).
        """
        from glitchhunter_ai.config import (
            DEAL_MAX_AGE_HOURS,
            DEAL_MIN_SAVINGS_CLP,
            DEAL_PRIORITY_MIN_DISCOUNT,
            DEAL_PUBLISH_BATCH,
            DEAL_TECH_MIN_DISCOUNT,
            DEAL_TECH_MIN_SAVINGS_CLP,
        )

        published = 0
        try:
            rows = self.db.get_commercial_deals(
                min_discount=min_discount,
                max_discount=max_discount,
                limit=50000,   # todas las candidatas: la consulta ya filtra ahorro y publicadas
                max_age_hours=DEAL_MAX_AGE_HOURS,
                min_savings=DEAL_MIN_SAVINGS_CLP,
                unpublished_only=True,
            )
            # Segunda pasada: Apple y la watchlist desde un descuento menor (DEAL_PRIORITY_MIN_DISCOUNT)
            terms = self._priority_terms()
            priority_rows: List[Dict[str, Any]] = []
            if terms and DEAL_PRIORITY_MIN_DISCOUNT < min_discount:
                priority = self.db.get_commercial_deals(
                    min_discount=DEAL_PRIORITY_MIN_DISCOUNT,
                    max_discount=min_discount,
                    limit=5000,
                    max_age_hours=DEAL_MAX_AGE_HOURS,
                    min_savings=DEAL_MIN_SAVINGS_CLP,
                    unpublished_only=True,
                    title_terms=terms,
                )
                priority_rows = [r for r in priority if self._priority_match(str(r.get("title") or ""), terms)]
            # Tercera pasada: tecnología y línea blanca desde 25% con ahorro sustantivo en pesos
            tech_rows: List[Dict[str, Any]] = []
            if DEAL_TECH_MIN_DISCOUNT < min_discount:
                raw_tech = self.db.get_commercial_deals(
                    min_discount=DEAL_TECH_MIN_DISCOUNT,
                    max_discount=min_discount,
                    limit=5000,
                    max_age_hours=DEAL_MAX_AGE_HOURS,
                    min_savings=max(DEAL_MIN_SAVINGS_CLP, DEAL_TECH_MIN_SAVINGS_CLP),
                    unpublished_only=True,
                )
                for r in raw_tech:
                    cat = classify_category(str(r.get("title") or ""))
                    if cat not in NON_TECH_CATEGORIES and cat != ACCESSORY_CATEGORY and cat != "otros":
                        tech_rows.append(r)
            # Primero prioridad (Apple/watchlist), luego >=40%, luego tech 25%–39%, sin duplicar
            seen_pids: set = set()
            combined: List[Dict[str, Any]] = []
            for r in (priority_rows + list(rows) + tech_rows):
                pid = str(r.get("product_id") or "")
                if pid and pid not in seen_pids:
                    seen_pids.add(pid)
                    combined.append(r)
            rows = combined
            checked = 0
            for row in rows:
                current = float(row.get("current_price") or 0.0)
                card = row.get("card_price")
                best = float(card) if card and 0 < float(card) < current else current
                # Los rechazados por el filtro de calidad (accesorios, reacondicionados…) no se reevalúan
                # a este precio: si no, ocuparían siempre los primeros cupos de cada pasada.
                key = (str(row.get("product_id")), round(best))
                if key in self._rejected_deals:
                    continue
                if checked >= DEAL_PUBLISH_BATCH:
                    break
                checked += 1
                if not self._is_publishable_deal(row, best):
                    self._rejected_deals[key] = None
                    if len(self._rejected_deals) > self._rejected_deals_max:
                        self._rejected_deals.popitem(last=False)
                    continue
                # Categoría consistente para los filtros de la página (los scrapers usan rótulos distintos)
                row = dict(row, category=classify_category(str(row.get("title") or "")))
                post_id = self.db.publish_feed_offer(
                    kind="deal",
                    product_id=row["product_id"],
                    store_id=row["store_id"],
                    best_price=best,
                    discount_pct=float(row.get("discount_pct") or 0.0),
                    payload=row,
                )
                if post_id:
                    published += 1
        except Exception as exc:
            logger.warning(f"No se pudieron publicar las ofertas en el canal: {exc}")

        if published:
            logger.info(f"📢 [Canal] {published} oferta(s) nueva(s) publicada(s).")
            if self.on_glitch_callback:
                try:
                    self.on_glitch_callback({"type": "feed_new", "new_posts": published})
                except Exception:
                    pass
        return published

    def run_direct_scan(
        self,
        keywords: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        send_notifications: bool = True,
    ) -> List[GlitchAlert]:
        """
        Escaneo directo y frecuente de Falabella y Ripley (modo híbrido): términos de la watchlist
        y categorías prioritarias, con precios en tiempo real. Solo libera `_scan_lock` durante
        descargas HTTP para no frenar al lector continuo.
        """
        from glitchhunter_ai.config import DIRECT_FETCH_WORKERS, DIRECT_PCFACTORY_PAGES_PER_CYCLE

        if not self._orchestrator_lock.acquire(blocking=False):
            logger.debug("Escaneo directo omitido: hay otro ciclo de escaneo en curso.")
            self.last_direct_scan_skipped = True
            return []

        if not self._scan_lock.acquire(blocking=False):
            self._orchestrator_lock.release()
            logger.debug("Escaneo directo omitido: hay un procesamiento en curso.")
            self.last_direct_scan_skipped = True
            return []
        self._scan_lock.release()

        self.last_direct_scan_skipped = False
        self.is_scanning = True
        detected: List[GlitchAlert] = []
        started = time.monotonic()
        try:
            if keywords is None:
                keywords = self.db.get_watchlist_keywords()
            categories = categories or []

            def fetch_store(store_id: str) -> List[List[ProductItem]]:
                batches: List[List[ProductItem]] = []
                for kw in keywords:
                    batches.append(self.direct_reader.search(store_id, kw))
                for cat in categories:
                    batches.append(self.direct_reader.fetch_category(store_id, cat))
                # Tiendas con API de catálogo completo (PC Factory, VTEX): se recorre por tramos en cada ciclo
                if hasattr(self.direct_reader, "fetch_catalog_slice"):
                    batches.append(self.direct_reader.fetch_catalog_slice(store_id, pages=DIRECT_PCFACTORY_PAGES_PER_CYCLE))
                return batches

            # Cada tienda es un sitio distinto: se consultan en paralelo fuera de _scan_lock.
            fetched: Dict[str, List[List[ProductItem]]] = {}
            with ThreadPoolExecutor(max_workers=DIRECT_FETCH_WORKERS, thread_name_prefix="direct") as pool:
                from glitchhunter_ai.config import store_enabled
                futures = {pool.submit(fetch_store, s): s for s in DIRECT_STORES if store_enabled(s)}
                for fut in as_completed(futures):
                    store_id = futures[fut]
                    try:
                        fetched[store_id] = fut.result()
                    except Exception as exc:
                        logger.warning("⚠️ [Escaneo Directo] %s falló y se omite en este ciclo: %s", store_id, exc)
                        fetched[store_id] = []

            seen: set = set()
            with self._scan_lock:
                self._bulk_price_counts = {}
                for store_id in DIRECT_STORES:
                    batches = fetched.get(store_id, [])
                    store_products: List[ProductItem] = []
                    for products in batches:
                        for prod in products:
                            if prod.id in seen:
                                continue
                            seen.add(prod.id)
                            store_products.append(prod)
                    self._note_bulk_prices(store_products)
                    for prod in store_products:
                        alert = self.process_product(prod, send_notifications=send_notifications)
                        if alert:
                            detected.append(alert)
                self._bulk_price_counts = {}

                logger.info(
                    "⚡ [Escaneo Directo] %d producto(s) revisados en tiempo real en %s, %d alerta(s) nueva(s) (%.0f s).",
                    len(seen), ", ".join(STORE_NAMES[s] for s in DIRECT_STORES), len(detected), time.monotonic() - started,
                )
                self.publish_new_deals()
                self.refresh_feed_statuses()
            return detected
        finally:
            self.is_scanning = False
            self._orchestrator_lock.release()

    def count_fresh_deals(self, products: List[ProductItem], min_discount: float = 40.0) -> int:
        """Productos con ≥min_discount% que son nuevos en el radar o bajaron de precio desde la última lectura."""
        candidates = [p for p in products if p.normal_price > 0
                      and (p.normal_price - p.best_price) / p.normal_price * 100 >= min_discount]
        if not candidates:
            return 0
        known = self.db.get_best_prices([p.id for p in candidates])
        return sum(1 for p in candidates if p.id not in known or p.best_price < known[p.id] - 0.5)

    def process_store_products(
        self,
        store_id: str,
        products: List[ProductItem],
        send_notifications: bool = True,
        lock_timeout: float = 120.0,
        latency_ms: Optional[float] = None,
    ) -> tuple:
        """
        Procesa la lectura de UNA tienda (lectores independientes por tienda). Espera su turno en el
        cerrojo en vez de saltarse la lectura. Retorna (alertas, ofertas frescas) o ([], 0) si no hubo turno.
        """
        unique: Dict[str, ProductItem] = {}
        for prod in products:
            unique.setdefault(prod.id, prod)
        products = list(unique.values())
        if not products:
            return [], 0
        if self.forwarder is not None:
            if latency_ms is not None:
                self.forwarder.submit(store_id, products, latency_ms=latency_ms)
            else:
                self.forwarder.submit(store_id, products)
            if self.forward_only:
                if not self._scan_lock.acquire(timeout=lock_timeout):
                    return [], 0
                try:
                    fresh = 0
                    for p in products:
                        prev = self._forward_seen_prices.get(p.id)
                        if p.normal_price > 0 and (p.normal_price - p.best_price) / p.normal_price * 100 >= 40.0:
                            if prev is None or p.best_price < prev - 0.5:
                                fresh += 1
                        self._forward_seen_prices[p.id] = p.best_price
                    if len(self._forward_seen_prices) > 50000:
                        self._forward_seen_prices.clear()
                    return [], fresh
                finally:
                    self._scan_lock.release()
        if not self._scan_lock.acquire(timeout=lock_timeout):
            logger.warning("⚠️ [Lector %s] %d producto(s) descartados: cerrojo ocupado.", store_id, len(products))
            return [], 0
        detected: List[GlitchAlert] = []
        try:
            fresh = self.count_fresh_deals(products)
            baseline_map: Dict[str, Optional[float]] = {}
            already_upserted = False
            if (
                isinstance(self.db, PriceDatabase)
                and getattr(type(self.db), "upsert_product", None) is PriceDatabase.upsert_product
                and "upsert_product" not in getattr(self.db, "__dict__", {})
            ):
                baseline_map = {
                    p.id: self.db.get_baseline_price(p.id, days=30)
                    for p in products
                    if p.in_stock
                }
                self.db.upsert_products_batch(products)
                already_upserted = True
            self._bulk_price_counts = {}
            self._note_bulk_prices(products)
            for prod in products:
                alert = self.process_product(
                    prod,
                    send_notifications=send_notifications,
                    _already_upserted=already_upserted,
                    _baseline_price=baseline_map.get(prod.id),
                )
                if alert:
                    detected.append(alert)
            return detected, fresh
        finally:
            self._bulk_price_counts = {}
            self._scan_lock.release()

    def publish_tick(self) -> int:
        """Publica en el canal las ofertas nuevas y revisa la vigencia (lo llama el lector continuo)."""
        if not self._scan_lock.acquire(timeout=120.0):
            return 0
        try:
            published = self.publish_new_deals()
            self.refresh_feed_statuses()
            return published
        finally:
            self._scan_lock.release()

    def run_watchlist_scan(
        self,
        use_live: bool = False,
        send_notifications: Optional[bool] = None,
    ) -> List[GlitchAlert]:
        """
        Escaneo de la watchlist bajo demanda (API / CLI). Serializa contra otros ciclos
        y libera `_scan_lock` durante consultas de red.
        """
        if not self._orchestrator_lock.acquire(blocking=False):
            logger.warning("⚠️ Escaneo de watchlist omitido: ya hay un escaneo en curso.")
            return []
        if not self._scan_lock.acquire(blocking=False):
            self._orchestrator_lock.release()
            logger.warning("⚠️ Escaneo de watchlist omitido: cerrojo ocupado.")
            return []
        self._scan_lock.release()
        self.is_scanning = True
        try:
            return self._run_watchlist_scan_unlocked(use_live=use_live, send_notifications=send_notifications)
        finally:
            self.is_scanning = False
            self._orchestrator_lock.release()

    def _run_watchlist_scan_unlocked(
        self,
        use_live: bool = False,
        send_notifications: Optional[bool] = None,
    ) -> List[GlitchAlert]:
        """
        Escaneo prioritario y proactivo de los términos en la Watchlist del usuario.
        En modo LIVE: busca ofertas en Knasta/SoloTodo ordenadas por precio ascendente.
        En modo DEMO: genera candidatos de simulación que coincidan con los términos.
        """
        if send_notifications is None:
            send_notifications = bool(use_live)

        keywords = self.db.get_watchlist_keywords()
        if not keywords:
            logger.debug("ℹ️ [Watchlist Scan] No hay palabras clave registradas en la watchlist.")
            return []

        logger.info(
            "🎯 [Watchlist Scan] Iniciando escaneo prioritario para %d término(s): %s (live=%s)",
            len(keywords),
            ", ".join(keywords),
            use_live,
        )

        detected_watchlist_alerts: List[GlitchAlert] = []
        from glitchhunter_ai.scrapers.live_feed import LiveRetailFeed
        live_feed = LiveRetailFeed()

        for kw in keywords:
            matched_products: List[ProductItem] = []
            if use_live:
                matched_products = live_feed.search_live_products(query=kw, order="price_asc", limit=20)
            else:
                matched_products = self._generate_simulated_watchlist_products(kw)

            with self._scan_lock:
                for prod in matched_products:
                    alert = self.process_product(prod, send_notifications=send_notifications)
                    if alert:
                        detected_watchlist_alerts.append(alert)

        logger.info(
            "🎯 [Watchlist Scan] Concluido: %d alertas prioritarias confirmadas para la Watchlist.",
            len(detected_watchlist_alerts),
        )
        return detected_watchlist_alerts

    def _generate_simulated_watchlist_products(self, keyword: str) -> List[ProductItem]:
        """Genera productos simulados de alta fidelidad que coinciden con la palabra clave para tests y demo."""
        clean_kw = keyword.strip()
        uid = uuid.uuid4().hex[:6].upper()
        now = datetime.now(timezone.utc)
        return [
            ProductItem(
                id=f"test-wl-{uid.lower()}",
                store_id="pcfactory",
                store_name="PC Factory",
                title=f"{clean_kw.title()} Special Edition [TEST-{uid}]",
                category="componentes",
                current_price=119990.0,
                normal_price=1199990.0,
                card_price=109990.0,
                url=f"https://www.pcfactory.cl/producto/wl-{uid.lower()}",
                image_url="https://www.pcfactory.cl/public/foto/rtx.jpg",
                in_stock=True,
                timestamp=now,
            )
        ]

    def _notify_store_health_alert(self, store_id: str, store_name: str):
        """Emite alerta de salud operativa cuando un scraper devuelve 0 productos reiteradamente."""
        msg = (
            f"⚠️ [ALERTA DE SISTEMA] {store_name} ({store_id}) ha devuelto 0 productos en 3 escaneos consecutivos. "
            f"Posible bloqueo anti-bot o cambio de estructura en la tienda."
        )
        logger.warning(msg)
        try:
            server_url = getattr(self.ntfy_dispatcher, "server_url", None) or getattr(self.ntfy_dispatcher, "server", None)
            topic = getattr(self.ntfy_dispatcher, "topic", None)
            if getattr(self.ntfy_dispatcher, "enabled", False) and server_url and topic:
                import requests
                ascii_name = str(store_name).encode("ascii", "ignore").decode("ascii").strip() or store_id
                headers = {
                    "Title": f"Alerta Salud Retailer: {ascii_name}",
                    "Priority": "high",
                    "Tags": "warning,rotating_light",
                }
                requests.post(
                    f"{str(server_url).rstrip('/')}/{topic}",
                    data=msg.encode("utf-8"),
                    headers=headers,
                    timeout=5,
                )
        except Exception as exc:
            logger.debug(f"Error enviando alerta de salud por ntfy: {exc}")

    def print_glitch_summary_table(self, glitches: List[GlitchAlert]):
        """Renderiza una tabla ejecutiva de glitches detectados con formato Rich."""
        if not glitches:
            console.print("[bold yellow]No se detectaron glitches críticos en esta ejecución.[/bold yellow]")
            return

        table = Table(
            title="🎯 GlitchHunter AI - Glitches Críticos Detectados en Retail Chileno",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Tienda", style="cyan", width=14)
        table.add_column("Producto", style="white", max_width=38)
        table.add_column("Precio Normal", justify="right", style="dim")
        table.add_column("Precio Bug", justify="right", style="bold green")
        table.add_column("Ahorro CLP", justify="right", style="bold yellow")
        table.add_column("Desc.", justify="right", style="bold red")
        table.add_column("Severidad", justify="center")
        table.add_column("Missing Zero", justify="center")

        for g in glitches:
            p = g.product
            v = g.verdict
            best_price = p.card_price or p.current_price
            missing_tag = "🎯 SÍ (~10x)" if v.is_missing_zero else "—"
            sev_style = "bold red" if v.severity == GlitchSeverity.CRITICO else "bold yellow"

            table.add_row(
                p.store_name,
                p.title[:36] + ("..." if len(p.title) > 36 else ""),
                f"${p.normal_price:,.0f} CLP".replace(",", "."),
                f"${best_price:,.0f} CLP".replace(",", "."),
                f"${v.absolute_savings_clp:,.0f} CLP".replace(",", "."),
                f"-{v.discount_percentage:.0f}%",
                f"[{sev_style}]{v.severity.value}[/{sev_style}]",
                missing_tag,
            )

        console.print(table)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    pipeline = GlitchHuntingPipeline()
    glitches = pipeline.run_scan(use_live=False)
    pipeline.print_glitch_summary_table(glitches)
