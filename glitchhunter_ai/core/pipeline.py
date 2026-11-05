"""
GlitchHunter AI - Pipeline Orquestador Multietapa
Coordina la extracción de datos desde los 6 scrapers de retail chileno,
el cálculo de anomalías matemáticas (descuentos masivos y Missing Zero Bug),
la verificación de inflación artificial (precios falsos de CyberDay)
y el filtro semántico NLP estricto para erradicar el 'Carcasa Trap'.
"""

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
from glitchhunter_ai.core.price_signals import BULK_MIN_PRODUCTS, detect_bulk_prices
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
    r"segunda\s+selecci[oó]n|exhibici[oó]n|outlet)\b",
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
        from glitchhunter_ai.core.catalog_rotator import default_catalog_rotator
        self.catalog_rotator = default_catalog_rotator
        self._scan_lock = threading.Lock()
        # Productos con el mismo precio que otros de su tienda en el escaneo en curso (carga masiva)
        self._bulk_price_counts: Dict[str, int] = {}
        self.last_direct_scan_skipped = False   # True si el último escaneo directo se omitió por otro en curso
        self.is_scanning = False

    def process_product(
        self,
        product: ProductItem,
        send_notifications: bool = True,
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

        # Fase 1: Consulta de precio histórico base previo (30 días) y persistencia
        baseline_price = self.db.get_baseline_price(product.id, days=30)
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

        # ⏱️ Confirmación en tiempo real: los precios del agregador llegan con ~40 min de retraso.
        # Antes de alertar se relee el producto directamente en la tienda (Falabella / Ripley).
        if (
            self.confirm_with_store
            and product.store_id in DIRECT_STORES
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
        # El precio tachado lo pone la tienda: perfumes y cremas muestran "-70%" todo el año.
        # Sin evidencia, el producto se publica como oferta (sin push), no como error de precio.
        best_now = product.best_price
        bulk_count = self._bulk_price_counts.get(product.id, 0)
        verified_drop = bool(
            math_eval.get("is_missing_zero")                                        # ~10x: firma clásica de error
            or bulk_count >= BULK_MIN_PRODUCTS                                      # mismo precio en muchos productos
            or (baseline_price and baseline_price > 0 and best_now <= baseline_price * 0.5)  # nuestro historial
            or (mkt_ref and not market_inflation and mkt_ref.market_discount_pct >= 50.0)  # otras tiendas
            or product.source == "knasta"                                           # vs precio habitual de Knasta
        )

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
        confidence = float(math_eval.get("confidence_score", 95.0))

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
            reason=(
                f"{'⚠️ Missing Zero Bug (~10x)' if missing_zero else f'Descuento masivo {discount_pct:.1f}%'} "
                f"en {product.store_name} | Ahorro: ${savings_clp:,.0f} CLP".replace(",", ".")
                + (" | ⚠️ Precio normal inflado vs mercado" if market_inflation else "")
                + (f" | 📦 Mismo precio en {bulk_count} productos (posible error de carga masiva)" if bulk_count else "")
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
        is_watchlist = self.db.is_in_watchlist(product.title)
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

        if send_notifications and not is_test_product and not market_inflation and verified_drop:
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
        # El escaneo automático espera a que termine un escaneo directo en curso (en vez de perder el ciclo);
        # un escaneo manual vía API no espera y responde "ocupado".
        acquired = (
            self._scan_lock.acquire(timeout=wait_for_lock_seconds)
            if wait_for_lock_seconds > 0
            else self._scan_lock.acquire(blocking=False)
        )
        if not acquired:
            logger.warning("⚠️ Intento de escaneo concurrente rechazado: ya existe un ciclo de escaneo en ejecución.")
            return []

        self.is_scanning = True
        self._bulk_price_counts = {}
        all_detected_glitches: List[GlitchAlert] = []

        try:
            if send_notifications is None:
                send_notifications = bool(use_live)

            # Expirar ofertas que no han sido vistas en las últimas 12 horas
            try:
                expired = self.db.expire_stale_deals(max_hours=12)
                if expired > 0:
                    logger.info(f"⏳ [Expiración Automática] {expired} oferta(s) no vistas en >12h transicionadas a EXPIRED.")
            except Exception as e:
                logger.debug(f"Error en expire_stale_deals: {e}")

            target_scrapers = [
                s for s in self.scrapers
                if store_ids is None or s.store_id in store_ids
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
                    total_scanned += len(products)
                    self._note_bulk_prices(products)
                    for prod in products:
                        alert = self.process_product(prod, send_notifications=send_notifications)
                        if alert:
                            all_detected_glitches.append(alert)
                except Exception as exc:
                    has_error = True
                    logger.error(f"Error escaneando {scraper.store_name}: {exc}")
                finally:
                    elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                    global_store_health.record_scan(
                        store_id=scraper.store_id,
                        store_name=scraper.store_name,
                        latency_ms=elapsed_ms,
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
                self.publish_new_deals()
                self.refresh_feed_statuses()
            return all_detected_glitches
        finally:
            self.is_scanning = False
            self._scan_lock.release()

    run_scan_cycle = run_scan

    def _note_bulk_prices(self, products: List[ProductItem]) -> None:
        """Registra los productos que comparten precio exacto con otros de su tienda (carga masiva)."""
        counts = detect_bulk_prices(products)
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
        return changed

    def _publish_unverified_as_deal(self, product: ProductItem, discount_pct: float, savings_clp: float) -> None:
        """Publica como 'Oferta' un descuento ≥65% sin evidencia independiente (si pasa el filtro de calidad)."""
        row = {
            "product_id": product.id,
            "store_id": product.store_id,
            "title": product.title,
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
        if REFURBISHED_RE.search(title):
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

    def publish_new_deals(self, min_discount: float = 40.0, max_discount: float = 65.0) -> int:
        """
        Publica en el canal las ofertas comerciales (40%-64%) que aún no se habían publicado,
        o que ahora están más baratas que en su última publicación. Las ya publicadas no se repiten.
        Solo pasan las que superan el filtro de calidad (ver _is_publishable_deal).
        """
        from glitchhunter_ai.config import DEAL_MAX_AGE_HOURS

        published = 0
        try:
            rows = self.db.get_commercial_deals(
                min_discount=min_discount,
                max_discount=max_discount,
                limit=1000,
                max_age_hours=DEAL_MAX_AGE_HOURS,
            )
            for row in rows:
                current = float(row.get("current_price") or 0.0)
                card = row.get("card_price")
                best = float(card) if card and 0 < float(card) < current else current
                if not self._is_publishable_deal(row, best):
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
                    self.on_glitch_callback({"type": "feed_updated", "new_posts": published})
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
        y categorías prioritarias, con precios en tiempo real. Comparte el cerrojo con run_scan,
        por lo que se omite si hay un escaneo completo en curso.
        """
        from glitchhunter_ai.config import DIRECT_FETCH_WORKERS, DIRECT_PCFACTORY_PAGES_PER_CYCLE

        if not self._scan_lock.acquire(blocking=False):
            logger.debug("Escaneo directo omitido: hay un escaneo en curso.")
            self.last_direct_scan_skipped = True
            return []

        self.last_direct_scan_skipped = False
        self.is_scanning = True
        self._bulk_price_counts = {}
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

            # Cada tienda es un sitio distinto: se consultan en paralelo (la red es ~80% del escaneo).
            # El procesamiento y la base de datos siguen en este hilo, en el orden de DIRECT_STORES.
            fetched: Dict[str, List[List[ProductItem]]] = {}
            with ThreadPoolExecutor(max_workers=DIRECT_FETCH_WORKERS, thread_name_prefix="direct") as pool:
                futures = {pool.submit(fetch_store, s): s for s in DIRECT_STORES}
                for fut in as_completed(futures):
                    store_id = futures[fut]
                    try:
                        fetched[store_id] = fut.result()
                    except Exception as exc:
                        logger.warning("⚠️ [Escaneo Directo] %s falló y se omite en este ciclo: %s", store_id, exc)
                        fetched[store_id] = []

            seen: set = set()
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

            logger.info(
                "⚡ [Escaneo Directo] %d producto(s) revisados en tiempo real en %s, %d alerta(s) nueva(s) (%.0f s).",
                len(seen), ", ".join(STORE_NAMES[s] for s in DIRECT_STORES), len(detected), time.monotonic() - started,
            )
            self.publish_new_deals()
            self.refresh_feed_statuses()
            return detected
        finally:
            self.is_scanning = False
            self._scan_lock.release()

    def run_watchlist_scan(
        self,
        use_live: bool = False,
        send_notifications: Optional[bool] = None,
    ) -> List[GlitchAlert]:
        """
        Escaneo de la watchlist bajo demanda (API / CLI). Toma el mismo cerrojo que run_scan:
        si hay un escaneo en curso se omite, en vez de correr dos escaneos en paralelo.
        """
        if not self._scan_lock.acquire(blocking=False):
            logger.warning("⚠️ Escaneo de watchlist omitido: ya hay un escaneo en curso.")
            return []
        self.is_scanning = True
        try:
            return self._run_watchlist_scan_unlocked(use_live=use_live, send_notifications=send_notifications)
        finally:
            self.is_scanning = False
            self._scan_lock.release()

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
            if hasattr(self.ntfy_dispatcher, "enabled") and self.ntfy_dispatcher.enabled and getattr(self.ntfy_dispatcher, "topic", None):
                import requests
                headers = {
                    "Title": f"⚠️ Alerta Salud Retailer: {store_name}",
                    "Priority": "high",
                    "Tags": "warning,rotating_light",
                }
                requests.post(
                    f"{self.ntfy_dispatcher.server}/{self.ntfy_dispatcher.topic}",
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
