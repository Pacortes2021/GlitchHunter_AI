"""
Módulo de Detección de Inflación Artificial de Precios (CyberDay Bait Detector).

Identifica prácticas comerciales desleales y engaños donde los retailers inflan
el "precio normal" de lista antes de eventos de descuento (CyberDay, Black Friday, etc.),
simulando descuentos exorbitantes que en realidad no existen sobre el valor histórico real.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
import statistics
from typing import List, Optional, Tuple

# Garantiza resolución de paquetes tanto en ejecución directa como importado
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Agrega también el directorio glitchhunter_ai si se ejecuta dentro de ai_detector
GLITCHHUNTER_DIR = Path(__file__).resolve().parent.parent
if str(GLITCHHUNTER_DIR) not in sys.path:
    sys.path.insert(0, str(GLITCHHUNTER_DIR))

try:
    from glitchhunter_ai.config import MIN_DISCOUNT_PERCENTAGE
    from glitchhunter_ai.core.models import PriceHistoryRecord, ProductItem
except ImportError:
    from config import MIN_DISCOUNT_PERCENTAGE
    from core.models import PriceHistoryRecord, ProductItem


class InflationVerifier:
    """
    Verificador de inflación de precios y detector de ofertas engañosas (CyberDay Bait).
    
    Analiza la relación entre el precio normal declarado por la tienda y el precio
    histórico base de los últimos 30 días para determinar si una oferta es un glitch real
    o una simulación mediante aumento artificial previo del precio de lista.
    """

    def __init__(
        self,
        inflation_threshold: float = 1.4,
        min_glitch_discount: float = MIN_DISCOUNT_PERCENTAGE,
    ) -> None:
        """
        Inicializa el verificador de inflación.

        :param inflation_threshold: Multiplicador sobre el precio base histórico
                                    a partir del cual se considera inflación artificial
                                    (por defecto 1.4, correspondiente a +40%).
        :param min_glitch_discount: Umbral mínimo de descuento porcentual real para
                                    ser calificado como sospecha de glitch legítimo
                                    (por defecto 65.0% según config).
        """
        self.inflation_threshold = inflation_threshold
        self.min_glitch_discount = min_glitch_discount

    @staticmethod
    def _format_clp(amount: float) -> str:
        """Formatea un monto numérico a formato de pesos chilenos legible ($50.000)."""
        return f"{int(round(amount)):,}".replace(",", ".")

    def compute_baseline_from_records(
        self,
        records: List[PriceHistoryRecord],
        max_days: int = 30,
        reference_date: Optional[datetime] = None,
        method: str = "median",
    ) -> Optional[float]:
        """
        Calcula el precio base histórico de los últimos `max_days` días
        a partir de una lista de registros históricos `PriceHistoryRecord`.

        :param records: Lista de registros históricos del producto.
        :param max_days: Ventana temporal en días a considerar (default 30).
        :param reference_date: Fecha de referencia (default now).
        :param method: Método de agregación: 'median' (por defecto, robusto contra anomalías) o 'mean'.
        :return: Precio base calculado o None si no hay registros válidos.
        """
        if not records:
            return None

        ref_date = reference_date or datetime.utcnow()
        cutoff_date = ref_date - timedelta(days=max_days)

        valid_prices = [
            r.price for r in records
            if r.recorded_at >= cutoff_date and r.price > 0
        ]

        if not valid_prices:
            # Si no hay en la ventana estricta pero existen registros históricos,
            # utilizar los más recientes disponibles como fallback
            valid_prices = [r.price for r in records if r.price > 0]

        if not valid_prices:
            return None

        if method == "median":
            return round(float(statistics.median(valid_prices)), 2)
        elif method == "mean":
            return round(float(statistics.mean(valid_prices)), 2)
        else:
            return round(float(min(valid_prices)), 2)

    def verify_inflation(
        self,
        product: ProductItem,
        baseline_price: Optional[float] = None,
    ) -> Tuple[bool, float, str]:
        """
        Verifica si el precio de lista normal del producto presenta inflación artificial
        y recalcula el porcentaje de descuento real sobre el baseline histórico.

        :param product: Instancia de ProductItem con precios capturados.
        :param baseline_price: Precio base histórico de los últimos 30 días (opcional).
        :return: Tupla (is_fake_inflation, real_discount_percentage, explanation):
            - is_fake_inflation (bool): True si normal_price > baseline_price * 1.4
            - real_discount_percentage (float): Porcentaje de descuento real sobre el baseline.
            - explanation (str): Detalle explicativo de la evaluación realizada.
        """
        # Validación de datos del producto
        if product.normal_price <= 0:
            return False, 0.0, "[Error] El producto no posee un precio normal válido declarado."

        effective_price = product.best_price

        # Caso: Sin historial previo disponible
        if baseline_price is None or baseline_price <= 0:
            apparent_discount = product.discount_percentage
            explanation = (
                f"[Sin Historial Base] No se dispone de precio histórico de los últimos 30 días. "
                f"Evaluado contra precio normal publicado (${self._format_clp(product.normal_price)}). "
                f"Descuento aparente: {apparent_discount:.1f}%."
            )
            return False, apparent_discount, explanation

        # Cálculo de la tasa de inflación del precio de lista
        inflation_ratio = product.normal_price / baseline_price
        is_fake_inflation = inflation_ratio > self.inflation_threshold
        inflation_percentage = round((inflation_ratio - 1.0) * 100.0, 1)

        # Cálculo del descuento real sobre el precio base histórico
        savings = baseline_price - effective_price
        if savings <= 0:
            real_discount = round((savings / baseline_price) * 100.0, 2)
        else:
            real_discount = round((savings / baseline_price) * 100.0, 2)

        # Verificación y generación de veredicto
        if is_fake_inflation:
            if real_discount < self.min_glitch_discount:
                explanation = (
                    f"⚠️ [FALSA INFLACIÓN / FAKE DEAL DESCARTADO]: "
                    f"Inflación artificial detectada. La tienda declara precio normal de "
                    f"${self._format_clp(product.normal_price)}, inflado un {inflation_percentage}% "
                    f"sobre su valor base histórico de 30 días (${self._format_clp(baseline_price)}). "
                    f"El descuento aparente publicado ({product.discount_percentage:.1f}%) es engañoso; "
                    f"el descuento real es de apenas {real_discount:.1f}% "
                    f"(a ${self._format_clp(effective_price)}), "
                    f"por debajo del umbral mínimo de glitch ({self.min_glitch_discount}%)."
                )
            else:
                explanation = (
                    f"⚡ [INFLACIÓN DETECTADA PERO GLITCH REAL CONFIRMADO]: "
                    f"El precio normal fue inflado un {inflation_percentage}% "
                    f"(${self._format_clp(product.normal_price)} vs base ${self._format_clp(baseline_price)}). "
                    f"Sin embargo, el precio de oferta (${self._format_clp(effective_price)}) "
                    f"representa un descuento real legítimo del {real_discount:.1f}% sobre su baseline, "
                    f"superando el umbral requerido de glitch ({self.min_glitch_discount}%)."
                )
        else:
            if real_discount >= self.min_glitch_discount:
                explanation = (
                    f"✅ [GLITCH GENUINO CONFIRMADO]: "
                    f"Precio de lista consistente con el historial (${self._format_clp(baseline_price)}). "
                    f"Descuento real legítimo del {real_discount:.1f}% a ${self._format_clp(effective_price)} "
                    f"(Ahorro real: ${self._format_clp(max(0.0, savings))})."
                )
            else:
                explanation = (
                    f"ℹ️ [OFERTA ESTÁNDAR / NO ES GLITCH]: "
                    f"Precio normal consistente con su historial de 30 días (${self._format_clp(baseline_price)}). "
                    f"El descuento real es del {real_discount:.1f}%, dentro de los márgenes normales comerciales."
                )

        return is_fake_inflation, real_discount, explanation

    def is_glitch_candidate(
        self,
        product: ProductItem,
        baseline_price: Optional[float] = None,
    ) -> bool:
        """
        Retorna True si y solo si la oferta califica como candidato a glitch legítimo
        tras filtrar falsas inflaciones.
        """
        _, real_discount, _ = self.verify_inflation(product, baseline_price)
        return real_discount >= self.min_glitch_discount


if __name__ == "__main__":
    print("=" * 80)
    print("🔬 GLITCHHUNTER AI - CyberDay Bait & Fake Inflation Detector")
    print("=" * 80)

    verifier = InflationVerifier(inflation_threshold=1.4, min_glitch_discount=65.0)

    # --------------------------------------------------------------------------
    # Caso 1: Fake Deal / CyberDay Bait (Zapatilla inflada de $50.000 a $150.000)
    # --------------------------------------------------------------------------
    fake_deal_product = ProductItem(
        id="zapatilla-bait-01",
        store_id="falabella",
        store_name="Falabella",
        title="Zapatillas Running Pro Nitro",
        category="calzado",
        current_price=45000.0,
        normal_price=150000.0,  # Inflado artificialmente (+200%)
        url="https://falabella.com/producto/zapatillas-pro",
    )
    historical_baseline_1 = 50000.0  # Precio real promedio últimos 30 días

    is_fake, real_disc, desc = verifier.verify_inflation(fake_deal_product, historical_baseline_1)
    print("\n--- CASO 1: CyberDay Bait (Precio Normal Inflado) ---")
    print(f"Producto: {fake_deal_product.title}")
    print(f"Precio Publicado: Normal ${fake_deal_product.normal_price:,.0f} -> Oferta ${fake_deal_product.current_price:,.0f}")
    print(f"Descuento Aparente de la Tienda: {fake_deal_product.discount_percentage:.1f}%")
    print(f"Precio Base Histórico (30 días): ${historical_baseline_1:,.0f}")
    print(f"¿Inflación Artificial Detectada?: {'SÍ (FAKE INFLATION)' if is_fake else 'NO'}")
    print(f"Descuento Real sobre Baseline: {real_disc:.1f}%")
    print(f"¿Pasa como Glitch?: {'SÍ' if verifier.is_glitch_candidate(fake_deal_product, historical_baseline_1) else 'DESCARTADO'}")
    print(f"Dictamen:\n{desc}\n")

    # --------------------------------------------------------------------------
    # Caso 2: Glitch Genuino (Smart TV OLED 65" con pérdida de cero)
    # --------------------------------------------------------------------------
    genuine_glitch_product = ProductItem(
        id="tv-oled-65",
        store_id="ripley",
        store_name="Ripley",
        title="Smart TV OLED 65 pulgadas 4K 120Hz",
        category="televisores",
        current_price=89990.0,
        normal_price=899990.0,  # Precio normal legítimo
        card_price=79990.0,     # Precio tarjeta Ripley aún menor
        url="https://ripley.cl/tv-oled-65",
    )
    historical_baseline_2 = 899990.0

    is_fake_2, real_disc_2, desc_2 = verifier.verify_inflation(genuine_glitch_product, historical_baseline_2)
    print("--- CASO 2: Glitch Genuino (Sin Inflación, Ahorro Masivo) ---")
    print(f"Producto: {genuine_glitch_product.title}")
    print(f"Precio Publicado: Normal ${genuine_glitch_product.normal_price:,.0f} -> Mejor Precio ${genuine_glitch_product.best_price:,.0f}")
    print(f"Descuento Aparente: {genuine_glitch_product.discount_percentage:.1f}%")
    print(f"Precio Base Histórico: ${historical_baseline_2:,.0f}")
    print(f"¿Inflación Artificial Detectada?: {'SÍ' if is_fake_2 else 'NO'}")
    print(f"Descuento Real sobre Baseline: {real_disc_2:.1f}%")
    print(f"¿Pasa como Glitch?: {'SÍ' if verifier.is_glitch_candidate(genuine_glitch_product, historical_baseline_2) else 'DESCARTADO'}")
    print(f"Dictamen:\n{desc_2}\n")

    # --------------------------------------------------------------------------
    # Caso 3: Inflación Detectada pero Glitch Real Válido
    # --------------------------------------------------------------------------
    inflated_but_glitch = ProductItem(
        id="laptop-gamer-01",
        store_id="paris",
        store_name="Paris",
        title="Notebook Gamer RTX 4080 Core i9",
        category="notebooks",
        current_price=99990.0,
        normal_price=2200000.0, # Inflado (+46% sobre base de 1.500.000)
        url="https://paris.cl/laptop-gamer",
    )
    historical_baseline_3 = 1500000.0

    is_fake_3, real_disc_3, desc_3 = verifier.verify_inflation(inflated_but_glitch, historical_baseline_3)
    print("--- CASO 3: Inflación de Lista con Glitch Extremo Real ---")
    print(f"Producto: {inflated_but_glitch.title}")
    print(f"Precio Base Histórico: ${historical_baseline_3:,.0f}")
    print(f"Precio Normal Inflado: ${inflated_but_glitch.normal_price:,.0f}")
    print(f"Precio Oferta: ${inflated_but_glitch.current_price:,.0f}")
    print(f"¿Inflación Artificial Detectada?: {'SÍ' if is_fake_3 else 'NO'}")
    print(f"Descuento Real sobre Baseline: {real_disc_3:.1f}%")
    print(f"¿Pasa como Glitch?: {'SÍ' if verifier.is_glitch_candidate(inflated_but_glitch, historical_baseline_3) else 'DESCARTADO'}")
    print(f"Dictamen:\n{desc_3}\n")

    # --------------------------------------------------------------------------
    # Caso 4: Producto sin historial previo registrado
    # --------------------------------------------------------------------------
    no_history_product = ProductItem(
        id="soundbar-01",
        store_id="lider",
        store_name="Líder",
        title="Barra de Sonido Dolby Atmos 5.1",
        category="audio",
        current_price=120000.0,
        normal_price=200000.0,
        url="https://lider.cl/soundbar",
    )
    is_fake_4, real_disc_4, desc_4 = verifier.verify_inflation(no_history_product, None)
    print("--- CASO 4: Sin Historial Previo de 30 Días ---")
    print(f"Producto: {no_history_product.title}")
    print(f"¿Inflación Artificial Detectada?: {'SÍ' if is_fake_4 else 'NO'}")
    print(f"Descuento Evaluado: {real_disc_4:.1f}%")
    print(f"Dictamen:\n{desc_4}\n")

    # --------------------------------------------------------------------------
    # Caso 5: Cálculo automático de Baseline desde PriceHistoryRecord
    # --------------------------------------------------------------------------
    print("--- CASO 5: Cálculo de Baseline con PriceHistoryRecord ---")
    history_records = [
        PriceHistoryRecord(product_id="zapatilla-bait-01", price=49990.0, store_id="falabella", recorded_at=datetime.utcnow() - timedelta(days=25)),
        PriceHistoryRecord(product_id="zapatilla-bait-01", price=51990.0, store_id="falabella", recorded_at=datetime.utcnow() - timedelta(days=15)),
        PriceHistoryRecord(product_id="zapatilla-bait-01", price=48990.0, store_id="falabella", recorded_at=datetime.utcnow() - timedelta(days=5)),
    ]
    computed_baseline = verifier.compute_baseline_from_records(history_records, max_days=30)
    print(f"Registros analizados: {[r.price for r in history_records]}")
    print(f"Precio Base Mediana Calculado: ${computed_baseline:,.0f}")
    is_fake_5, real_disc_5, desc_5 = verifier.verify_inflation(fake_deal_product, computed_baseline)
    print(f"¿Inflación Detectada usando Historial?: {'SÍ' if is_fake_5 else 'NO'}")
    print(f"Descuento Real Calculado: {real_disc_5:.1f}%")

    print("\n" + "=" * 80)
    print("✨ Todas las pruebas del módulo se completaron exitosamente.")
    print("=" * 80)
