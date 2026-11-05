"""
Módulo de Detección Matemática de Anomalías de Precios y Patrón de Omisión de Ceros (Missing Zero Bug).

Especializado en:
1. Cálculo estricto de descuentos porcentuales y ahorros absolutos en CLP respecto al
   precio de lista (normal_price) y precio histórico base (baseline_price).
2. Detección del patrón 'Missing Zero Bug' (omisión involuntaria de un dígito cero por
   error humano de tipeo o desincronización de ERP, caracterizado por un ratio de precio ~10x).
3. Evaluación de criterios de sospecha matemática de glitch (descuento >= 65% y ahorro >= $30.000 CLP,
   o presencia de Missing Zero Bug).
4. Asignación de un puntaje preliminar de confianza matemática calibrado (0 a 100).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Garantizar resolución de paquetes tanto en ejecución directa como importado
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

GLITCHHUNTER_DIR = Path(__file__).resolve().parent.parent
if str(GLITCHHUNTER_DIR) not in sys.path:
    sys.path.insert(0, str(GLITCHHUNTER_DIR))

try:
    from glitchhunter_ai.config import (
        CRITICAL_DISCOUNT_PERCENTAGE,
        MIN_ABSOLUTE_SAVINGS_CLP,
        MIN_DISCOUNT_PERCENTAGE,
        MISSING_ZERO_MIN_REFERENCE_CLP,
        MISSING_ZERO_RATIO_MAX,
        MISSING_ZERO_RATIO_MIN,
    )
    from glitchhunter_ai.core.models import GlitchSeverity, ProductItem
except ImportError:
    from config import (
        CRITICAL_DISCOUNT_PERCENTAGE,
        MIN_ABSOLUTE_SAVINGS_CLP,
        MIN_DISCOUNT_PERCENTAGE,
        MISSING_ZERO_MIN_REFERENCE_CLP,
        MISSING_ZERO_RATIO_MAX,
        MISSING_ZERO_RATIO_MIN,
    )
    from core.models import GlitchSeverity, ProductItem


class MathAnomalyDetector:
    """
    Motor matemático de detección de anomalías de precios para retail chileno.

    Evalúa discrepancias extremas entre precios actuales y precios de lista o de referencia,
    detectando de manera especializada el 'Missing Zero Bug' (omisión de ceros ~10x).
    """

    def __init__(
        self,
        min_discount_percentage: float = MIN_DISCOUNT_PERCENTAGE,
        critical_discount_percentage: float = CRITICAL_DISCOUNT_PERCENTAGE,
        min_absolute_savings_clp: float = MIN_ABSOLUTE_SAVINGS_CLP,
        missing_zero_ratio_min: float = MISSING_ZERO_RATIO_MIN,
        missing_zero_ratio_max: float = MISSING_ZERO_RATIO_MAX,
        missing_zero_min_reference: float = MISSING_ZERO_MIN_REFERENCE_CLP,
    ) -> None:
        """
        Inicializa el detector con umbrales configurables.

        :param min_discount_percentage: Descuento porcentual mínimo para sospecha (default: 65.0%).
        :param critical_discount_percentage: Umbral de descuento para considerar severidad crítica (default: 85.0%).
        :param min_absolute_savings_clp: Ahorro mínimo en pesos chilenos para justificar alerta (default: $30.000 CLP).
        :param missing_zero_ratio_min: Límite inferior del ratio para Missing Zero Bug (default: 8.5x).
        :param missing_zero_ratio_max: Límite superior del ratio para Missing Zero Bug (default: 11.5x).
        """
        self.min_discount_percentage = float(min_discount_percentage)
        self.critical_discount_percentage = float(critical_discount_percentage)
        self.min_absolute_savings_clp = float(min_absolute_savings_clp)
        self.missing_zero_ratio_min = float(missing_zero_ratio_min)
        self.missing_zero_ratio_max = float(missing_zero_ratio_max)
        self.missing_zero_min_reference = float(missing_zero_min_reference)

    def detect_missing_zero(
        self,
        reference_price: float,
        current_price: float,
    ) -> Tuple[bool, Optional[float]]:
        """
        Detecta si el ratio entre el precio de referencia y el precio actual corresponde
        al patrón de omisión de un cero (Missing Zero Bug).

        En el retail, cuando un operador comete un 'typo' al digitar $899.990 como $89.990,
        o una integración omite un cero, el ratio reference_price / current_price oscila
        generalmente entre 8.5x y 11.5x (~10x).

        :param reference_price: Precio de referencia (normal_price o baseline_price).
        :param current_price: Precio de venta ofertado actual.
        :return: Tupla (is_missing_zero: bool, ratio: Optional[float]).
        """
        if current_price <= 0.0 or reference_price <= 0.0:
            return False, None

        ratio = reference_price / current_price
        is_missing = (self.missing_zero_ratio_min <= ratio <= self.missing_zero_ratio_max
                      and reference_price >= self.missing_zero_min_reference)
        return is_missing, round(ratio, 2)

    def calculate_discount_and_savings(
        self,
        reference_price: float,
        current_price: float,
    ) -> Tuple[float, float]:
        """
        Calcula el porcentaje de descuento y el ahorro monetario en CLP.

        :param reference_price: Precio original de lista o baseline histórico.
        :param current_price: Precio efectivo de venta.
        :return: Tupla (discount_percentage: float, absolute_savings_clp: float).
        """
        if reference_price <= 0.0:
            return 0.0, 0.0

        savings = max(0.0, reference_price - current_price)
        discount_pct = (savings / reference_price) * 100.0 if savings > 0 else 0.0
        return round(discount_pct, 2), round(savings, 2)

    def calculate_confidence_score(
        self,
        is_glitch_candidate: bool,
        is_missing_zero: bool,
        discount_pct: float,
        savings_clp: float,
        missing_zero_ratio: Optional[float] = None,
        digit_match: bool = False,
        baseline_contradiction: bool = False,
    ) -> float:
        """
        Calcula un puntaje de confianza matemática calibrado de 0.0 a 100.0.

        Factores de ponderación:
        - Si no es candidato: puntaje bajo proporcional al descuento comercial (0 a 35 pts).
        - Missing Zero Bug: base de 85 puntos + bonificación por cercanía a 10.0x (+10 pts) + coincidencia de prefijo (+3 pts).
        - Descuento extremo: base de 65 puntos + hasta 20 puntos por descuento (>=85% crítico) + hasta 15 puntos por volumen de ahorro.
        - Penalización si el historial base demuestra que el precio normal fue inflado artificialmente.

        :return: Puntaje de confianza matemática en el rango [0.0, 100.0].
        """
        if not is_glitch_candidate:
            if discount_pct <= 0.0 or savings_clp <= 0.0:
                return 0.0
            # Descuento comercial estándar: puntaje proporcional bajo
            return round(min(35.0, (discount_pct / self.min_discount_percentage) * 35.0), 1)

        score = 60.0

        if is_missing_zero:
            score = 85.0
            if missing_zero_ratio is not None:
                # Bonificación por proximidad exacta al ratio 10.0x
                ratio_diff = abs(missing_zero_ratio - 10.0)
                if ratio_diff <= 0.2:
                    score += 9.0
                elif ratio_diff <= 0.6:
                    score += 6.0
                elif ratio_diff <= 1.2:
                    score += 3.0

            if digit_match:
                score += 3.0

            if savings_clp >= 100000.0:
                score += 2.0
        else:
            # Descuento tradicional severo
            if discount_pct >= 95.0:
                score += 22.0
            elif discount_pct >= self.critical_discount_percentage:  # >= 85%
                score += 16.0
            elif discount_pct >= 75.0:
                score += 10.0
            elif discount_pct >= self.min_discount_percentage:      # >= 65%
                score += 4.0

            # Bonificación por magnitud del ahorro absoluto en CLP
            if savings_clp >= 500000.0:
                score += 12.0
            elif savings_clp >= 200000.0:
                score += 8.0
            elif savings_clp >= 50000.0:
                score += 4.0

        # Penalización si el baseline histórico contradice el precio normal (inflación artificial previa)
        if baseline_contradiction:
            score -= 35.0

        return round(min(100.0, max(0.0, score)), 1)

    def evaluate(
        self,
        product: ProductItem,
        baseline_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evalúa matemáticamente un producto para detectar anomalías de precios y omisión de ceros.

        Calcula métricas clave tanto respecto al precio normal de lista del producto
        como al precio base histórico (si se provee), determina si califica como
        sospecha de glitch y asigna la severidad y confianza matemática correspondiente.

        :param product: Instancia de ProductItem con precios y detalles del producto.
        :param baseline_price: Precio histórico base opcional (calculado desde BD o mediana).
        :return: Diccionario estructurado con el resultado del diagnóstico matemático.
        """
        effective_price = float(product.best_price)
        normal_price = float(product.normal_price)

        # 1. Descuento y ahorro respecto al normal_price
        discount_normal, savings_normal = self.calculate_discount_and_savings(
            reference_price=normal_price,
            current_price=effective_price,
        )

        # 2. Descuento y ahorro respecto al baseline_price (si existe)
        has_baseline = baseline_price is not None and baseline_price > 0.0
        if has_baseline:
            discount_baseline, savings_baseline = self.calculate_discount_and_savings(
                reference_price=float(baseline_price),
                current_price=effective_price,
            )
        else:
            discount_baseline = None
            savings_baseline = None

        # 3. Detección del Missing Zero Bug (~10x ratio)
        # Evaluamos contra normal_price y contra baseline_price
        mz_normal, ratio_normal = self.detect_missing_zero(normal_price, effective_price)
        mz_baseline, ratio_baseline = (
            self.detect_missing_zero(float(baseline_price), effective_price)
            if has_baseline
            else (False, None)
        )

        # Evaluamos también contra current_price regular por si difiere de best_price (ej. precio tarjeta)
        mz_curr_normal, ratio_curr_normal = (False, None)
        if product.card_price is not None and product.current_price != effective_price:
            mz_curr_normal, ratio_curr_normal = self.detect_missing_zero(
                normal_price, float(product.current_price)
            )

        is_missing_zero = mz_normal or mz_baseline or mz_curr_normal

        # Seleccionar el ratio más representativo para el reporte
        active_ratio: Optional[float] = None
        if mz_normal:
            active_ratio = ratio_normal
        elif mz_baseline:
            active_ratio = ratio_baseline
        elif mz_curr_normal:
            active_ratio = ratio_curr_normal
        elif ratio_normal is not None:
            active_ratio = ratio_normal
        elif ratio_baseline is not None:
            active_ratio = ratio_baseline

        # Verificación auxiliar de coincidencia de dígitos iniciales (ej: '899990' vs '89990' -> prefijo '89')
        digit_match = False
        ref_for_digit = normal_price if normal_price > 0 else (float(baseline_price) if has_baseline else 0.0)
        if is_missing_zero and ref_for_digit > 0 and effective_price > 0:
            ref_str = str(int(ref_for_digit))
            cur_str = str(int(effective_price))
            if len(ref_str) >= 2 and len(cur_str) >= 2 and ref_str[:2] == cur_str[:2]:
                digit_match = True

        # 4. Evaluación de Sospecha Matemática de Glitch
        # Regla: (descuento >= 65% y ahorro >= $30.000 CLP) O Missing Zero Bug
        meets_normal_threshold = (
            discount_normal >= self.min_discount_percentage
            and savings_normal >= self.min_absolute_savings_clp
        )
        meets_baseline_threshold = (
            has_baseline
            and discount_baseline is not None
            and savings_baseline is not None
            and discount_baseline >= self.min_discount_percentage
            and savings_baseline >= self.min_absolute_savings_clp
        )

        is_glitch_candidate = is_missing_zero or meets_normal_threshold or meets_baseline_threshold

        # Detección de posible contradicción si baseline histórico indica que el descuento es falso (CyberDay bait)
        baseline_contradiction = False
        if has_baseline and discount_baseline is not None:
            if meets_normal_threshold and discount_baseline < 20.0 and not is_missing_zero:
                baseline_contradiction = True

        # 5. Descuento y ahorro representativos del dictamen
        primary_discount = discount_normal if normal_price > 0.0 else (discount_baseline or 0.0)
        primary_savings = savings_normal if normal_price > 0.0 else (savings_baseline or 0.0)
        reference_price = normal_price if normal_price > 0.0 else (float(baseline_price) if has_baseline else 0.0)

        # 6. Cálculo del Puntaje de Confianza Matemática
        confidence_score = self.calculate_confidence_score(
            is_glitch_candidate=is_glitch_candidate,
            is_missing_zero=is_missing_zero,
            discount_pct=primary_discount,
            savings_clp=primary_savings,
            missing_zero_ratio=active_ratio,
            digit_match=digit_match,
            baseline_contradiction=baseline_contradiction,
        )

        # 7. Determinación de Severidad Sugerida
        if is_missing_zero or primary_discount >= self.critical_discount_percentage:
            severity = GlitchSeverity.CRITICO
        elif is_glitch_candidate:
            severity = GlitchSeverity.ALTO
        elif primary_discount >= 45.0:
            severity = GlitchSeverity.MODERADO
        else:
            severity = GlitchSeverity.FALSO_POSITIVO

        # 8. Generación de Explicación Detallada (Reason)
        if is_missing_zero:
            ratio_display = f"{active_ratio:.2f}x" if active_ratio is not None else "~10x"
            reason = (
                f"🚨 Sospecha de Glitch Crítico [Missing Zero Bug]: Ratio de precio {ratio_display} (~10x). "
                f"Precio ref: ${reference_price:,.0f} vs actual: ${effective_price:,.0f}. "
                f"Ahorro de ${primary_savings:,.0f} CLP ({primary_discount:.1f}% dcto)."
            )
        elif is_glitch_candidate:
            if primary_discount >= self.critical_discount_percentage:
                reason = (
                    f"🔥 Sospecha de Glitch Crítico [Descuento Masivo]: {primary_discount:.1f}% de descuento "
                    f"(umbral crítico: {self.critical_discount_percentage:.1f}%) con un ahorro de ${primary_savings:,.0f} CLP."
                )
            else:
                reason = (
                    f"⚠️ Sospecha de Glitch [Descuento Alto]: {primary_discount:.1f}% de descuento "
                    f"con ahorro de ${primary_savings:,.0f} CLP (supera umbral {self.min_discount_percentage:.1f}% y $30.000 CLP)."
                )
        elif primary_discount > 0.0:
            reason = (
                f"ℹ️ Descuento Comercial Estándar: {primary_discount:.1f}% de descuento "
                f"(${primary_savings:,.0f} CLP) no alcanza el umbral de glitch (>= {self.min_discount_percentage:.1f}% y $30.000 CLP)."
            )
        else:
            reason = "✅ Precio Regular: Sin descuento registrado respecto al precio de lista."

        return {
            "is_glitch_candidate": is_glitch_candidate,
            "is_suspicious": is_glitch_candidate,            # Alias de compatibilidad
            "is_math_glitch": is_glitch_candidate,           # Alias de compatibilidad
            "is_missing_zero": is_missing_zero,
            "confidence_score": confidence_score,
            "discount_percentage": primary_discount,
            "discount_normal": discount_normal,
            "discount_percentage_normal": discount_normal,   # Alias
            "discount_baseline": discount_baseline,
            "discount_percentage_baseline": discount_baseline, # Alias
            "baseline_discount_percentage": discount_baseline, # Alias
            "absolute_savings_clp": primary_savings,
            "savings_normal": savings_normal,
            "savings_baseline": savings_baseline,
            "baseline_absolute_savings_clp": savings_baseline, # Alias
            "effective_price": effective_price,
            "normal_price": normal_price,
            "baseline_price": float(baseline_price) if has_baseline else None,
            "reference_price": reference_price,
            "missing_zero_ratio": active_ratio,
            "severity_hint": severity.value,
            "severity": severity,
            "reason": reason,
            "details": {
                "meets_normal_threshold": meets_normal_threshold,
                "meets_baseline_threshold": meets_baseline_threshold,
                "digit_match": digit_match,
                "baseline_contradiction": baseline_contradiction,
                "min_discount_threshold": self.min_discount_percentage,
                "min_savings_threshold": self.min_absolute_savings_clp,
                "card_price_used": product.card_price is not None and product.best_price == product.card_price,
            },
        }


if __name__ == "__main__":
    from datetime import datetime

    print("=" * 80)
    print("🔎 GlitchHunter AI — Verificación de MathAnomalyDetector")
    print("=" * 80)

    detector = MathAnomalyDetector()

    # --------------------------------------------------------------------------
    # CASO 1: Caso Normal (Sin descuento o precio igual a precio de lista)
    # --------------------------------------------------------------------------
    product_normal = ProductItem(
        id="tv-samsung-normal",
        store_id="falabella",
        store_name="Falabella",
        title="Smart TV Samsung 55 UHD 4K",
        category="televisores",
        current_price=399990.0,
        normal_price=399990.0,
        url="https://falabella.com/producto/1",
        in_stock=True,
    )
    result_normal = detector.evaluate(product_normal)
    print("\n--- CASO 1: Producto Normal (Sin Descuento) ---")
    print(f"Producto: {product_normal.title}")
    print(f"Precio Normal: ${product_normal.normal_price:,.0f} | Oferta: ${product_normal.current_price:,.0f}")
    print(f"Descuento: {result_normal['discount_percentage']:.1f}% | Ahorro: ${result_normal['absolute_savings_clp']:,.0f} CLP")
    print(f"¿Sospecha de Glitch?: {'SÍ' if result_normal['is_glitch_candidate'] else 'NO'}")
    print(f"¿Missing Zero?: {'SÍ' if result_normal['is_missing_zero'] else 'NO'}")
    print(f"Confianza Matemática: {result_normal['confidence_score']}/100")
    print(f"Severidad: {result_normal['severity_hint']}")
    print(f"Dictamen: {result_normal['reason']}")

    # --------------------------------------------------------------------------
    # CASO 2: Descuento Comercial Real Moderado (30% de descuento)
    # --------------------------------------------------------------------------
    product_moderate = ProductItem(
        id="audifonos-sony-30pct",
        store_id="paris",
        store_name="Paris",
        title="Audífonos Bluetooth Sony WH-CH520",
        category="audio",
        current_price=34990.0,
        normal_price=49990.0,  # ~30% dcto, ahorro $15.000 CLP
        url="https://paris.cl/producto/2",
        in_stock=True,
    )
    result_moderate = detector.evaluate(product_moderate)
    print("\n--- CASO 2: Descuento Comercial Moderado (~30%) ---")
    print(f"Producto: {product_moderate.title}")
    print(f"Precio Normal: ${product_moderate.normal_price:,.0f} | Oferta: ${product_moderate.current_price:,.0f}")
    print(f"Descuento: {result_moderate['discount_percentage']:.1f}% | Ahorro: ${result_moderate['absolute_savings_clp']:,.0f} CLP")
    print(f"¿Sospecha de Glitch?: {'SÍ' if result_moderate['is_glitch_candidate'] else 'NO'}")
    print(f"¿Missing Zero?: {'SÍ' if result_moderate['is_missing_zero'] else 'NO'}")
    print(f"Confianza Matemática: {result_moderate['confidence_score']}/100")
    print(f"Severidad: {result_moderate['severity_hint']}")
    print(f"Dictamen: {result_moderate['reason']}")

    # --------------------------------------------------------------------------
    # CASO 3: Glitch Extremo por Descuento Masivo (88% - Sin Missing Zero)
    # --------------------------------------------------------------------------
    product_extreme = ProductItem(
        id="macbook-pro-glitch-88pct",
        store_id="ripley",
        store_name="Ripley",
        title="Apple MacBook Pro 14 M3 Pro 18GB 512GB",
        category="notebooks",
        current_price=120000.0,
        normal_price=1000000.0,  # 88.0% dcto, ahorro $880.000 CLP (ratio 8.33x < 8.5x, puro descuento)
        url="https://ripley.cl/producto/3",
        in_stock=True,
    )
    result_extreme = detector.evaluate(product_extreme)
    print("\n--- CASO 3: Glitch Extremo por Descuento Masivo (88% - Sin Missing Zero) ---")
    print(f"Producto: {product_extreme.title}")
    print(f"Precio Normal: ${product_extreme.normal_price:,.0f} | Oferta: ${product_extreme.current_price:,.0f}")
    print(f"Descuento: {result_extreme['discount_percentage']:.1f}% | Ahorro: ${result_extreme['absolute_savings_clp']:,.0f} CLP")
    print(f"¿Sospecha de Glitch?: {'SÍ' if result_extreme['is_glitch_candidate'] else 'NO'}")
    print(f"¿Missing Zero?: {'SÍ' if result_extreme['is_missing_zero'] else 'NO'} (Ratio: {result_extreme['missing_zero_ratio']:.2f}x)")
    print(f"Confianza Matemática: {result_extreme['confidence_score']}/100")
    print(f"Severidad: {result_extreme['severity_hint']}")
    print(f"Dictamen: {result_extreme['reason']}")

    # --------------------------------------------------------------------------
    # CASO 4: Patrón de Omisión de Ceros (Missing Zero Bug: $899.990 -> $89.990)
    # --------------------------------------------------------------------------
    product_missing_zero = ProductItem(
        id="samsung-galaxy-s24-missing-zero",
        store_id="falabella",
        store_name="Falabella",
        title="Smartphone Samsung Galaxy S24 Ultra 256GB Titanium Gray",
        category="smartphones",
        current_price=89990.0,
        normal_price=899990.0,  # Falta un cero: $899.990 -> $89.990 (ratio ~10x)
        url="https://falabella.com/producto/4",
        in_stock=True,
    )
    result_missing_zero = detector.evaluate(product_missing_zero)
    print("\n--- CASO 4: Missing Zero Bug ($899.990 -> $89.990) ---")
    print(f"Producto: {product_missing_zero.title}")
    print(f"Precio Normal: ${product_missing_zero.normal_price:,.0f} | Oferta: ${product_missing_zero.current_price:,.0f}")
    print(f"Ratio Detectado: {result_missing_zero['missing_zero_ratio']:.2f}x")
    print(f"¿Missing Zero?: {'SÍ' if result_missing_zero['is_missing_zero'] else 'NO'}")
    print(f"Descuento: {result_missing_zero['discount_percentage']:.1f}% | Ahorro: ${result_missing_zero['absolute_savings_clp']:,.0f} CLP")
    print(f"¿Sospecha de Glitch?: {'SÍ' if result_missing_zero['is_glitch_candidate'] else 'NO'}")
    print(f"Confianza Matemática: {result_missing_zero['confidence_score']}/100")
    print(f"Severidad: {result_missing_zero['severity_hint']}")
    print(f"Dictamen: {result_missing_zero['reason']}")

    # --------------------------------------------------------------------------
    # CASO 5: Evaluación con Precio Base Histórico (Baseline)
    # --------------------------------------------------------------------------
    product_baseline = ProductItem(
        id="consola-ps5-baseline",
        store_id="pcfactory",
        store_name="PC Factory",
        title="Consola PlayStation 5 Slim 1TB Standard",
        category="consolas",
        current_price=54990.0,
        normal_price=0.0,  # Tienda no reportó normal_price, pero hay histórico
        url="https://pcfactory.cl/producto/5",
        in_stock=True,
    )
    baseline_history_price = 549990.0  # Histórico registrado de ~$550.000 (ratio 10x)
    result_baseline = detector.evaluate(product_baseline, baseline_price=baseline_history_price)
    print("\n--- CASO 5: Evaluación con Baseline Histórico ($549.990 -> $54.990) ---")
    print(f"Producto: {product_baseline.title}")
    print(f"Baseline Histórico: ${baseline_history_price:,.0f} | Oferta: ${product_baseline.current_price:,.0f}")
    print(f"Descuento sobre Baseline: {result_baseline['discount_baseline']:.1f}% | Ahorro: ${result_baseline['savings_baseline']:,.0f} CLP")
    print(f"Ratio Detectado: {result_baseline['missing_zero_ratio']:.2f}x")
    print(f"¿Missing Zero?: {'SÍ' if result_baseline['is_missing_zero'] else 'NO'}")
    print(f"¿Sospecha de Glitch?: {'SÍ' if result_baseline['is_glitch_candidate'] else 'NO'}")
    print(f"Confianza Matemática: {result_baseline['confidence_score']}/100")
    print(f"Dictamen: {result_baseline['reason']}")

    print("\n" + "=" * 80)
    print("✨ Todas las pruebas del detector matemático se ejecutaron con éxito.")
    print("=" * 80)
