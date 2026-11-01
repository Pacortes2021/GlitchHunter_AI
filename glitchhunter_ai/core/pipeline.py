"""
GlitchHunter AI - Pipeline Orquestador Multietapa
Coordina la extracción de datos desde los 6 scrapers de retail chileno,
el cálculo de anomalías matemáticas (descuentos masivos y Missing Zero Bug),
la verificación de inflación artificial (precios falsos de CyberDay)
y el filtro semántico NLP estricto para erradicar el 'Carcasa Trap'.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.table import Table

from glitchhunter_ai.ai_detector.inflation_verifier import InflationVerifier
from glitchhunter_ai.ai_detector.math_detector import MathAnomalyDetector
from glitchhunter_ai.ai_detector.semantic_filter import SemanticGlitchFilter
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    ProductItem,
    SemanticClassificationResult,
)
from glitchhunter_ai.notifications import NtfyAlertDispatcher, TelegramAlertDispatcher
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
        on_glitch_callback: Optional[Callable[[GlitchAlert], Any]] = None,
    ):
        self.db = db or PriceDatabase()
        self.math_detector = MathAnomalyDetector()
        self.inflation_verifier = InflationVerifier()
        self.semantic_filter = SemanticGlitchFilter()
        self.telegram_dispatcher = telegram_dispatcher or TelegramAlertDispatcher()
        self.ntfy_dispatcher = ntfy_dispatcher or NtfyAlertDispatcher()
        self.scrapers = get_all_scrapers()
        self.on_glitch_callback = on_glitch_callback

    def process_product(self, product: ProductItem) -> Optional[GlitchAlert]:
        """
        Ejecuta el protocolo de análisis para un solo producto.
        Verifica stock, gestiona transiciones de estado (correcciones y caídas)
        y detecta anomalías matemáticas y de precios.
        """
        # Fase 0: Verificación de Stock (Productos sin stock no generan alertas)
        if not product.in_stock:
            if hasattr(self.db, "check_and_update_deal_status"):
                self.db.check_and_update_deal_status(product)
            logger.debug(f"📦 [Sin Stock Omitido] {product.title} ({product.id}) no tiene stock disponible.")
            return None

        # Fase 1: Consulta de precio histórico base previo (30 días) y persistencia
        baseline_price = self.db.get_baseline_price(product.id, days=30)
        self.db.upsert_product(product)

        # Fase 1.5: Verificación de corrección de ofertas activas previas
        # Si este producto ya estaba en tracked_deals y subió de precio, marcarlo como CORRECTED inmediatamente
        if hasattr(self.db, "check_and_update_deal_status"):
            prior_status = self.db.check_and_update_deal_status(product)
            if prior_status == "CORRECTED":
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

        # Fase 2: Detector Matemático de Anomalías (Porcentaje y Missing Zero Bug)
        math_eval = self.math_detector.evaluate(product, baseline_price=baseline_price)
        is_candidate = (
            math_eval.get("is_glitch_candidate")
            or math_eval.get("is_math_glitch")
            or math_eval.get("is_suspicious")
        )

        if not is_candidate:
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
            reason=(
                f"{'⚠️ Missing Zero Bug (~10x)' if missing_zero else f'Descuento masivo {discount_pct:.1f}%'} "
                f"en {product.store_name} | Ahorro: ${savings_clp:,.0f} CLP".replace(",", ".")
            ),
        )

        alert_id = f"ALT-{product.store_id.upper()}-{uuid.uuid4().hex[:8].upper()}"

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

        alert = GlitchAlert(
            alert_id=alert_id,
            product=product,
            verdict=verdict,
            alert_url=str(product.url),
            created_at=datetime.utcnow(),
            telegram_sent=False,
        )

        # Registro en BD SQLite
        try:
            self.db.record_glitch_alert(alert)
        except Exception as e:
            logger.warning(f"No se pudo registrar alerta en BD: {e}")

        # Despacho por Telegram (Modo Simulación o Live)
        try:
            dispatch_ok = self.telegram_dispatcher.send_alert(alert)
            if bool(dispatch_ok):
                self.db.mark_telegram_sent(alert.alert_id)
                alert.telegram_sent = True
        except Exception as e:
            logger.error(f"Error despachando alerta de Telegram para {alert_id}: {e}")

        # Despacho push a celular vía ntfy (con apertura automática de URL al presionar)
        try:
            self.ntfy_dispatcher.send_alert(alert)
        except Exception as e:
            logger.error(f"Error despachando notificación push ntfy para {alert_id}: {e}")

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
    ) -> List[GlitchAlert]:
        """
        Ejecuta un ciclo de escaneo completo a través de todos los scrapers seleccionados.
        """
        all_detected_glitches: List[GlitchAlert] = []
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

        logger.debug(f"🔎 Iniciando ciclo de escaneo en {len(target_scrapers)} tiendas retail...")

        total_scanned = 0
        for scraper in target_scrapers:
            try:
                products = scraper.fetch_catalog(use_live=use_live)
                total_scanned += len(products)
                for prod in products:
                    alert = self.process_product(prod)
                    if alert:
                        all_detected_glitches.append(alert)
            except Exception as exc:
                logger.error(f"Error escaneando {scraper.store_name}: {exc}")

        logger.debug(
            f"✅ Ciclo concluido: {total_scanned} productos analizados, "
            f"{len(all_detected_glitches)} glitches confirmados."
        )
        return all_detected_glitches

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
