"""
GlitchHunter AI - Pipeline Orquestador Multietapa
Coordina la extracción de datos desde los 6 scrapers de retail chileno,
el cálculo de anomalías matemáticas (descuentos masivos y Missing Zero Bug),
la verificación de inflación artificial (precios falsos de CyberDay)
y el filtro semántico NLP estricto para erradicar el 'Carcasa Trap'.
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from glitchhunter_ai.ai_detector.inflation_verifier import InflationVerifier
from glitchhunter_ai.ai_detector.math_detector import MathAnomalyDetector
from glitchhunter_ai.ai_detector.semantic_filter import SemanticGlitchFilter
from glitchhunter_ai.core.cart_sniper import generate_direct_add_to_cart_url
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

logger = logging.getLogger("GlitchHunter.Pipeline")
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
    ):
        self.db = db or PriceDatabase()
        self.math_detector = MathAnomalyDetector()
        self.inflation_verifier = InflationVerifier()
        self.semantic_filter = SemanticGlitchFilter()
        self.telegram_dispatcher = telegram_dispatcher or TelegramAlertDispatcher()
        self.ntfy_dispatcher = ntfy_dispatcher or NtfyAlertDispatcher()
        self.discord_dispatcher = discord_dispatcher or DiscordAlertDispatcher()
        self.scrapers = get_all_scrapers()
        self.on_glitch_callback = on_glitch_callback
        self._scan_lock = threading.Lock()
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
            if hasattr(self.db, "mark_deal_out_of_stock") and self.db.get_deal_status(product.id) == "ACTIVE":
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

        # 🌐 Consulta de Referencia de Mercado Inter-Tiendas (Solo candidatos pre-filtrados)
        mkt_ref = default_market_reference.get_market_reference(
            product=product,
            exclude_store_id=product.store_id,
            db=self.db,
        )

        # Señal 2: Verificación cruzada de inflación inter-tiendas
        if mkt_ref and mkt_ref.is_fake_inflation:
            is_fake_inflation = True
            inflation_explanation = (
                f"Inflación artificial inter-tiendas detectada: normal ${product.normal_price:,.0f} "
                f"> 1.8x mediana mercado ${mkt_ref.median_price:,.0f}."
            )
            logger.debug(f"🚫 [Inflación Cruzada Rechazada] {product.title} - {inflation_explanation}")
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
        if mkt_ref:
            if mkt_ref.market_discount_pct >= 60.0:
                confidence = min(99.0, confidence + 5.0)
            elif mkt_ref.market_discount_pct <= 15.0:
                confidence = max(40.0, confidence - 10.0)

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
            reason=(
                f"{'⚠️ Missing Zero Bug (~10x)' if missing_zero else f'Descuento masivo {discount_pct:.1f}%'} "
                f"en {product.store_name} | Ahorro: ${savings_clp:,.0f} CLP".replace(",", ".")
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
        if hasattr(self.db, "register_or_update_deal"):
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
        elif hasattr(self.db, "is_duplicate_alert") and self.db.is_duplicate_alert(product.id, product.best_price):
            logger.debug(f"🔁 [Glitch Activo Existente] Omitiendo duplicado de {product.title} ({product.id}).")
            return None

        # ⭐ REVISIÓN DE WATCHLIST: Prioridad especial sin corromper la certeza matemática (Hallazgo E de Auditoría 4)
        is_watchlist = False
        if hasattr(self.db, "is_in_watchlist") and self.db.is_in_watchlist(product.title):
            is_watchlist = True
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
            created_at=datetime.utcnow(),
            telegram_sent=False,
        )

        # Registro en BD SQLite
        try:
            self.db.record_glitch_alert(alert)
        except Exception as e:
            logger.warning(f"No se pudo registrar alerta en BD: {e}")

        # Despacho de notificaciones (si send_notifications está habilitado y no es producto de prueba)
        telegram_ok = False
        ntfy_ok = False
        discord_ok = False
        is_test_product = (
            product.id.startswith("test-")
            or product.store_id.startswith("test_")
            or getattr(product, "is_mock", False)
        )

        if send_notifications and not is_test_product:
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
            if hasattr(self.db, "mark_deal_dispatched"):
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
    ) -> List[GlitchAlert]:
        """
        Ejecuta un ciclo de escaneo completo a través de todos los scrapers seleccionados.
        Si use_live es False (modo simulación / demo), send_notifications es False por defecto,
        garantizando que ninguna alerta de prueba emita notificaciones push a redes reales.
        Protegido con un cerrojo atómico para impedir carreras por escaneos concurrentes (Hallazgo F).
        """
        if not self._scan_lock.acquire(blocking=False):
            logger.warning("⚠️ Intento de escaneo concurrente rechazado: ya existe un ciclo de escaneo en ejecución.")
            return []

        self.is_scanning = True
        all_detected_glitches: List[GlitchAlert] = []

        try:
            if send_notifications is None:
                send_notifications = bool(use_live)

            # Expirar ofertas que no han sido vistas en las últimas 12 horas
            if hasattr(self.db, "expire_stale_deals"):
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

            logger.debug(f"🔎 Iniciando ciclo de escaneo en {len(target_scrapers)} tiendas retail (live={use_live}, notify={send_notifications})...")

            total_scanned = 0
            for scraper in target_scrapers:
                t_start = time.perf_counter()
                has_error = False
                products = []
                try:
                    products = scraper.fetch_catalog(use_live=use_live)
                    total_scanned += len(products)
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

            logger.debug(
                f"✅ Ciclo concluido: {total_scanned} productos analizados, "
                f"{len(all_detected_glitches)} glitches confirmados."
            )
            return all_detected_glitches
        finally:
            self.is_scanning = False
            self._scan_lock.release()

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
