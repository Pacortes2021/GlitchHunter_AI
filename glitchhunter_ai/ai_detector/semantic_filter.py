"""
Filtro Semántico de Glitches y Detector Anti-Carcasas / Accesorios con IA y NLP Determinista.

Resuelve el dolor crítico en scrapers de e-commerce donde un accesorio
(ej: 'Funda Silicona para iPhone 15 Pro Max' a $9.990) es erróneamente clasificado
como un teléfono de gama alta con 98% de descuento.

Implementa:
1. Análisis morfosintáctico de sujeto vs. complemento de compatibilidad ('compatible con', 'para', etc.).
2. Ontología léxica especializada en retail tecnológico chileno (Falabella, Paris, Ripley, PC Factory, ML).
3. Motor híbrido: NLP determinista de alta precisión + soporte opcional para LLM.
4. Generación estricta de `SemanticClassificationResult`.
5. Evaluación de anomalía de precio para descarte automático de falsos positivos.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Garantiza resolución de paquetes tanto en ejecución directa como importado
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

GLITCHHUNTER_DIR = Path(__file__).resolve().parent.parent
if str(GLITCHHUNTER_DIR) not in sys.path:
    sys.path.insert(0, str(GLITCHHUNTER_DIR))

try:
    from glitchhunter_ai.core.models import ProductItem, SemanticClassificationResult
except ImportError:
    from core.models import ProductItem, SemanticClassificationResult


class SemanticGlitchFilter:
    """
    Clasificador semántico y filtro anti-falsos positivos de accesorios para GlitchHunter AI.
    
    Distingue con precisión quirúrgica cuándo un título publicado en retail corresponde
    a un equipo electrónico principal (smartphone, notebook, consola, tablet) frente a
    accesorios, fundas, cables, láminas protectoras o repuestos donde el dispositivo
    principal solo se menciona como referencia de compatibilidad.
    """

    # --------------------------------------------------------------------------
    # Ontología Léxica: Categorías de Accesorios y Términos Indicadores
    # --------------------------------------------------------------------------
    ACCESSORY_TAXONOMY: Dict[str, List[str]] = {
        "funda_carcasa": [
            "funda", "fundas", "carcasa", "carcasas", "case", "cases", "cover", "covers",
            "bumper", "bumpers", "holster", "sleeve", "estuche", "estuches", "pouch",
            "pouches", "cobertor", "cobertores", "protector antigolpe", "armor case",
            "flip cover", "book cover", "smart cover", "skin case", "housing", "shell",
            "carcasa trasera", "funda magnetica", "funda silicona", "case transparente",
            "case uso rudo", "funda cartera", "funda tpu"
        ],
        "lamina_mica_protector": [
            "mica", "micas", "lamina", "lámina", "laminas", "láminas", "vidrio templado",
            "tempered glass", "hidrogel", "hydrogel", "film protector", "protector de pantalla",
            "protector pantalla", "screen protector", "protector de lente", "protector lente",
            "protector de camara", "protector camara", "anillo protector", "lens protector",
            "cristal templado", "mica de privacidad", "lamina de privacidad", "privacidad 9d",
            "film hidrogel", "protector ceramico", "protector cerámico"
        ],
        "cable_adaptador": [
            "cable", "cables", "adaptador", "adaptadores", "convertidor", "convertidores",
            "dongle", "hub usb", "usb hub", "conector", "extensor usb", "cable otg",
            "cable lightning", "cable usb-c", "cable tipo c", "cable tipo-c",
            "cable micro usb", "cable hdmi", "cable auxiliar", "cable cargador",
            "cable datos", "alargue", "divisor", "splitter", "adaptador audio"
        ],
        "cargador_dock": [
            "cargador", "cargadores", "base de carga", "estacion de carga",
            "estación de carga", "dock de carga", "charging dock", "pad de carga",
            "cargador inalambrico", "cargador inalámbrico", "wireless charger",
            "cargador magnetico", "cargador magnético", "cargador de auto",
            "cargador mechero", "cargador de pared", "cargador generico",
            "cargador genérico", "fuente de poder", "power bank", "bateria externa"
        ],
        "correa_pulsera": [
            "correa", "correas", "pulsera", "pulseras", "brazalete", "strap",
            "straps", "watchband", "banda silicona", "malla milanesa",
            "pulsera de silicona", "pulsera eslabones", "extensible reloj",
            "brazalete deportivo", "eslabones"
        ],
        "soporte_stand": [
            "soporte", "soportes", "soporte de pared", "soporte pared", "soporte auto",
            "soporte para auto", "soporte celular", "soporte notebook", "mount",
            "wall mount", "stand", "holder", "base escritorio", "tripode",
            "trípode", "anillo soporte", "pop socket", "popsocket", "brazo articulado",
            "atril", "soporte ventilado"
        ],
        "repuesto_componente": [
            "repuesto", "repuestos", "pantalla lcd", "display lcd", "modulo pantalla",
            "módulo pantalla", "pantalla touch", "repuesto bateria", "repuesto batería",
            "bateria reemplazo", "batería reemplazo", "flex de carga", "teclado repuesto",
            "teclado reemplazo", "fan cooler repuesto", "tapa trasera repuesto",
            "bateria para", "pantalla para", "cristal para", "conector de carga"
        ],
        "skin_sticker": [
            "pegatina", "pegatinas", "sticker", "stickers", "skin", "skins",
            "calcomania", "calcomanía", "calcomanias", "calcomanías",
            "vinilo adhesivo", "skin protector", "decal", "adhesivo"
        ],
        "accesorio_gaming_audio": [
            "thumb grips", "grips mando", "almohadillas audifonos", "almohadillas de repuesto",
            "eartips", "funda mando", "funda joystick", "cubre analogo", "cubre análogo",
            "gatillos para celular", "gatillos mando", "base enfriamiento ps5"
        ],
    }

    # Modificadores de materiales y atributos típicos de accesorios
    ACCESSORY_MODIFIERS = [
        "silicona", "silicone", "tpu", "acrilico", "acrílico", "policarbonato",
        "cuero sintetico", "cuero sintético", "cuero pu", "simil cuero", "magsafe",
        "magnetica", "magnética", "antigolpes", "antigolpe", "antichoques",
        "transparente", "rigida", "rígida", "flexible", "glitter", "reforzada",
        "antideslizante", "graduable", "plegable", "ajustable", "ultra slim",
        "shockproof", "ultra fino", "9d", "11d", "mate", "antiespia", "antiespía"
    ]

    # Patrones sintácticos que introducen el "complemento de compatibilidad"
    COMPATIBILITY_REGEX = re.compile(
        r'\b(?:para|compatible\s+con|compatibles\s+con|diseñado\s+para|diseñada\s+para|'
        r'apto\s+para|apta\s+para|sirve\s+para|adaptable\s+a|for|fits|fits\s+for|made\s+for|'
        r'compat\s+con|exclusivo\s+para|diseñado\s+especialmente\s+para)\b',
        re.IGNORECASE
    )

    # Conectores de bundle genuino (donde el equipo principal es el producto y el accesorio un regalo)
    BUNDLE_REGEX = re.compile(
        r'\b(?:con\s+(?:funda|carcasa|lamina|regalo|auriculares|cargador)|'
        r'incluye\s+(?:funda|carcasa|lamina|mica|cable|cargador)|'
        r'\+\s*(?:funda|carcasa|mica|cable|cargador|regalo|juego|control)|'
        r'pack\s+celular|combo\s+consola|bundle)\b',
        re.IGNORECASE
    )

    # --------------------------------------------------------------------------
    # Ontología Léxica: Equipos Principales y Especificaciones de Alto Valor
    # --------------------------------------------------------------------------
    MAIN_DEVICE_KEYWORDS: Dict[str, List[str]] = {
        "smartphone": [
            "iphone", "galaxy", "smartphone", "celular", "telefono", "teléfono",
            "xiaomi", "redmi", "poco", "motorola", "moto g", "moto edge",
            "pixel", "honor", "oppo", "vivo", "oneplus", "huawei", "infinix"
        ],
        "notebook_laptop": [
            "macbook", "macbook pro", "macbook air", "notebook", "laptop",
            "thinkpad", "zenbook", "vivobook", "legion", "rog strix", "tuf gaming",
            "omen", "victus", "pavilion", "ideapad", "aspire", "swift",
            "galaxy book", "surface laptop", "dell xps", "alienware", "mac mini", "imac"
        ],
        "consola_videojuegos": [
            "playstation 5", "playstation 4", "ps5", "ps4", "xbox series x",
            "xbox series s", "xbox one", "nintendo switch", "steam deck",
            "rog ally", "consola"
        ],
        "tablet": [
            "ipad", "ipad pro", "ipad air", "ipad mini", "galaxy tab", "tablet",
            "redmi pad", "xiaomi pad"
        ],
        "smartwatch": [
            "apple watch", "galaxy watch", "smartwatch", "reloj inteligente",
            "garmin fenix", "garmin forerunner"
        ],
        "smart_tv": [
            "smart tv", "televisor", "tv oled", "tv qled", "neo qled", "crystal uhd"
        ],
    }

    # Especificaciones técnicas de hardware que respaldan dispositivo físico principal
    HARDWARE_SPECS_REGEX = re.compile(
        r'\b(?:(?:64|128|256|512)\s*(?:gb|gigas)|'
        r'(?:1|2)\s*tb|'
        r'(?:4|6|8|12|16|18|24|32|36|48|64)\s*gb\s*ram|'
        r'm[1-4](?:\s*(?:pro|max|ultra))?|'
        r'core\s*i[3579]|ryzen\s*[3579]|'
        r'rtx\s*(?:30|40)[5-9]0|'
        r'5g|dual\s*sim|esim|'
        r'(?:13\.3|14|14\.2|15\.6|16|16\.2|17\.3)\s*(?:"|\'\'|pulgadas|pul)|'
        r'titanio\s*(?:natural|negro|blanco|azul)?|'
        r'space\s*(?:black|gray)|gris\s*espacial|medianoche|starlight)\b',
        re.IGNORECASE
    )

    def __init__(
        self,
        use_llm: bool = False,
        min_confidence: float = 0.70,
        custom_llm_callable: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
    ) -> None:
        """
        Inicializa el filtro semántico anti-accesorios.

        :param use_llm: Si es True, permite delegar casos ambiguos a un LLM disponible.
        :param min_confidence: Umbral de confianza mínimo para clasificación automática.
        :param custom_llm_callable: Función opcional inyectable para llamadas de IA / LLM.
        """
        self.use_llm = use_llm
        self.min_confidence = min_confidence
        self.custom_llm_callable = custom_llm_callable
        self._flattened_accessory_terms = self._build_accessory_index()

    def _build_accessory_index(self) -> List[Tuple[str, str]]:
        """Construye un índice plano ordenado por longitud descendente de términos."""
        index: List[Tuple[str, str]] = []
        for category, terms in self.ACCESSORY_TAXONOMY.items():
            for term in terms:
                index.append((term.lower(), category))
        # Ordenar por longitud de mayor a menor para matchear primero términos compuestos
        index.sort(key=lambda x: len(x[0]), reverse=True)
        return index

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normaliza texto removiendo acentos diacríticos para búsqueda robusta."""
        nfkd = unicodedata.normalize("NFKD", text)
        return "".join([c for c in nfkd if not unicodedata.combining(c)]).lower()

    def classify(
        self,
        title: str,
        category: Optional[str] = None,
    ) -> SemanticClassificationResult:
        """
        Clasifica semánticamente un producto evaluando título y categoría.

        Retorna un objeto SemanticClassificationResult fuertemente tipado.
        """
        cleaned_title = title.strip()
        if not cleaned_title:
            return SemanticClassificationResult(
                is_accessory=False,
                is_main_device=False,
                detected_item_type="desconocido",
                explanation="Título vacío o no proporcionado.",
                confidence=0.0,
            )

        # Si se solicitó LLM y existe un backend configurado, intentar clasificación por IA
        if self.use_llm:
            llm_result = self._try_llm_classification(cleaned_title, category)
            if llm_result is not None:
                return llm_result

        # Motor de Alta Precisión NLP Determinista
        return self._classify_deterministic(cleaned_title, category)

    def _classify_deterministic(
        self,
        title: str,
        category: Optional[str] = None,
    ) -> SemanticClassificationResult:
        """
        Motor NLP determinista de alta precisión basado en ontología,
        posicionamiento sintáctico (Head Noun vs. Complemento) y detección de hardware.
        """
        norm_title = self._normalize_text(title)
        category_norm = self._normalize_text(category) if category else ""

        # ----------------------------------------------------------------------
        # Paso 1: Detección de Separadores de Compatibilidad ("para", "compatible con")
        # ----------------------------------------------------------------------
        compat_match = self.COMPATIBILITY_REGEX.search(norm_title)
        if compat_match:
            head_part = norm_title[:compat_match.start()].strip()
            target_part = norm_title[compat_match.end():].strip()

            # Analizar si la parte principal (Head) contiene indicadores de accesorio
            head_accessory_type, head_term = self._find_accessory_keyword(head_part)
            if head_accessory_type:
                target_device = self._detect_device_type(target_part)
                target_name = target_device if target_device != "desconocido" else target_part[:25]
                explanation = (
                    f"Accesorio detectado ('{head_term}' de tipo {head_accessory_type}) "
                    f"en la posición principal del título. El dispositivo '{target_name}' "
                    f"es únicamente el complemento de compatibilidad ({compat_match.group(0)}...)."
                )
                return SemanticClassificationResult(
                    is_accessory=True,
                    is_main_device=False,
                    detected_item_type=head_accessory_type,
                    explanation=explanation,
                    confidence=0.98,
                )

        # ----------------------------------------------------------------------
        # Paso 2: Búsqueda de Accesorios en todo el título
        # ----------------------------------------------------------------------
        accessory_type, matched_term = self._find_accessory_keyword(norm_title)

        # Verificar si es un bundle genuino donde el equipo es el sujeto principal
        # Ej: "iPhone 15 Pro Max 256GB + Funda de regalo"
        if accessory_type and self._is_genuine_main_device_bundle(norm_title, title):
            device_type = self._detect_device_type(norm_title)
            return SemanticClassificationResult(
                is_accessory=False,
                is_main_device=True,
                detected_item_type=device_type if device_type != "desconocido" else "smartphone",
                explanation=(
                    f"Bundle de hardware principal detectado ({device_type}) con especificaciones "
                    f"completas. El término de accesorio ('{matched_term}') corresponde a un artículo "
                    f"incluido de cortesía o adicional (+ regalo/combo)."
                ),
                confidence=0.92,
            )

        if accessory_type:
            # Si aparece un término de accesorio y no es un bundle del equipo principal,
            # en retail tecnológico se trata inequívocamente de un accesorio
            target_device = self._detect_device_type(norm_title)
            explanation = (
                f"Detectado término clave de accesorio '{matched_term}' ({accessory_type}). "
                f"El dispositivo mencionado ({target_device}) es el modelo de destino compatible."
            )
            return SemanticClassificationResult(
                is_accessory=True,
                is_main_device=False,
                detected_item_type=accessory_type,
                explanation=explanation,
                confidence=0.96,
            )

        # ----------------------------------------------------------------------
        # Paso 3: Identificación de Dispositivos Principales y Especificaciones
        # ----------------------------------------------------------------------
        device_type = self._detect_device_type(norm_title)
        has_hardware_specs = bool(self.HARDWARE_SPECS_REGEX.search(title))

        # Categoría declarada por el retail
        category_indicates_main = any(
            cat in category_norm
            for cat in ["smartphone", "celular", "notebook", "laptop", "consola", "computacion", "televisor"]
        )
        category_indicates_accessory = any(
            cat in category_norm
            for cat in ["accesorio", "funda", "carcasa", "cable", "adaptador", "audio accesorio"]
        )

        if category_indicates_accessory:
            return SemanticClassificationResult(
                is_accessory=True,
                is_main_device=False,
                detected_item_type="accesorio_generico",
                explanation=f"Categoría de retail indica expresamente accesorio ({category}).",
                confidence=0.90,
            )

        if device_type != "desconocido":
            confidence = 0.99 if has_hardware_specs else 0.94
            specs_note = " con especificaciones técnicas explícitas de hardware" if has_hardware_specs else ""
            explanation = (
                f"Dispositivo físico principal identificado ({device_type}){specs_note}. "
                f"No presenta términos de accesorio ni cláusulas de compatibilidad subordinada."
            )
            return SemanticClassificationResult(
                is_accessory=False,
                is_main_device=True,
                detected_item_type=device_type,
                explanation=explanation,
                confidence=confidence,
            )

        # ----------------------------------------------------------------------
        # Paso 4: Sin coincidencias explícitas (Caso Neutro o Genérico)
        # ----------------------------------------------------------------------
        return SemanticClassificationResult(
            is_accessory=False,
            is_main_device=False,
            detected_item_type="producto_general",
            explanation="No se encontraron indicadores concluyentes de accesorio ni dispositivo tecnológico de alto valor.",
            confidence=0.50,
        )

    def _find_accessory_keyword(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """Busca términos del índice de accesorios respetando límites de palabras completas."""
        for term, category in self._flattened_accessory_terms:
            pattern = r'\b' + re.escape(term) + r'\b'
            if re.search(pattern, text):
                return category, term
        return None, None

    def _detect_device_type(self, text: str) -> str:
        """Detecta la familia y tipo de dispositivo de alto valor mencionado en el texto."""
        for dev_type, keywords in self.MAIN_DEVICE_KEYWORDS.items():
            for kw in keywords:
                pattern = r'\b' + re.escape(kw) + r'\b'
                if re.search(pattern, text):
                    return dev_type
        return "desconocido"

    def _is_genuine_main_device_bundle(self, norm_title: str, raw_title: str) -> bool:
        """Determina si un título que contiene palabras de accesorio es un bundle del equipo."""
        has_device = self._detect_device_type(norm_title) != "desconocido"
        has_specs = bool(self.HARDWARE_SPECS_REGEX.search(raw_title))
        has_bundle_marker = bool(self.BUNDLE_REGEX.search(raw_title))
        return has_device and has_specs and has_bundle_marker

    # --------------------------------------------------------------------------
    # Evaluación de Glitches y Descarte de Precios Normales de Accesorios
    # --------------------------------------------------------------------------
    def evaluate_glitch_candidate(
        self,
        title: str,
        current_price: float,
        normal_price: Optional[float] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Evalúa integralmente si un producto publicado representa un glitch confirmado
        o si debe ser descartado por tratarse de un accesorio con precio normal de retail.

        :param title: Título publicado del producto.
        :param current_price: Precio actual publicado en CLP.
        :param normal_price: Precio de referencia o anterior (opcional).
        :param category: Categoría declarada por el retail (opcional).
        :return: Diccionario con resultado semántico, decisión de glitch y veredicto formateado.
        """
        semantic_res = self.classify(title, category)

        # Regla 2: Descarte de Accesorios a Precio Estándar
        # Un accesorio (carcasa, funda, mica, cable) a $9.990, $14.990, etc.,
        # corresponde a su precio de venta habitual, NO a un iPhone/MacBook con 95% de descuento.
        if semantic_res.is_accessory:
            is_glitch_candidate = False
            verdict_summary = "ACCESORIO (DESCARTADO / PRECIO NORMAL)"
            explanation = (
                f"Falsa alarma descartada: El producto es un accesorio ({semantic_res.detected_item_type}). "
                f"El precio de ${current_price:,.0f} CLP corresponde a un valor estándar de mercado para este ítem. "
                f"NO es un glitch ni un equipo principal."
            )
            return {
                "semantic_result": semantic_res,
                "is_glitch_candidate": is_glitch_candidate,
                "verdict_summary": verdict_summary,
                "status": "DESCARTADO",
                "explanation": explanation,
                "current_price": current_price,
                "normal_price": normal_price,
            }

        # Dispositivos Principales de Alto Valor (Smartphones, Laptops, Consolas)
        if semantic_res.is_main_device:
            # Si el precio es anómalamente bajo para un equipo principal (ej: < $300.000 para flagship/laptop)
            # o si el descuento es brutal respecto a un precio normal
            is_anomaly = False
            if current_price <= 350000.0:  # iPhone/MacBook a $119.990 o $249.990 es anomalía evidente
                is_anomaly = True
            elif normal_price and normal_price > 0:
                discount = ((normal_price - current_price) / normal_price) * 100.0
                if discount >= 65.0:
                    is_anomaly = True

            if is_anomaly:
                is_glitch_candidate = True
                verdict_summary = "DISPOSITIVO PRINCIPAL (GLITCH CONFIRMADO)"
                explanation = (
                    f"¡Alerta de Glitch Crítico Confirmada! El producto es un equipo principal de alto valor "
                    f"({semantic_res.detected_item_type}) publicado a ${current_price:,.0f} CLP, "
                    f"representando una anomalía severa de precio."
                )
                return {
                    "semantic_result": semantic_res,
                    "is_glitch_candidate": is_glitch_candidate,
                    "verdict_summary": verdict_summary,
                    "status": "GLITCH CONFIRMADO",
                    "explanation": explanation,
                    "current_price": current_price,
                    "normal_price": normal_price,
                }
            else:
                return {
                    "semantic_result": semantic_res,
                    "is_glitch_candidate": False,
                    "verdict_summary": "DISPOSITIVO PRINCIPAL (PRECIO DE MERCADO REGULAR)",
                    "status": "SIN GLITCH",
                    "explanation": f"Equipo principal ({semantic_res.detected_item_type}) a precio comercial normal (${current_price:,.0f} CLP).",
                    "current_price": current_price,
                    "normal_price": normal_price,
                }

        # Caso Genérico
        return {
            "semantic_result": semantic_res,
            "is_glitch_candidate": False,
            "verdict_summary": "PRODUCTO NO PRIORITARIO (DESCARTADO)",
            "status": "DESCARTADO",
            "explanation": "No califica como dispositivo principal de alto impacto.",
            "current_price": current_price,
            "normal_price": normal_price,
        }

    # --------------------------------------------------------------------------
    # Soporte Opcional para LLM (Anthropic / Custom Callable / OpenAI)
    # --------------------------------------------------------------------------
    def _try_llm_classification(
        self,
        title: str,
        category: Optional[str] = None,
    ) -> Optional[SemanticClassificationResult]:
        """Ejecuta clasificación vía LLM si está disponible, con fallback determinista."""
        if self.custom_llm_callable:
            try:
                res_dict = self.custom_llm_callable(title, category)
                return SemanticClassificationResult(**res_dict)
            except Exception:
                pass

        # Intento con Anthropic API si está la llave en el entorno
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=anthropic_key)
                prompt = (
                    f"Analiza este producto de retail tecnológico en Chile:\n"
                    f"Título: {title}\n"
                    f"Categoría: {category or 'No especificada'}\n\n"
                    f"Determina si es un ACCESORIO (funda, carcasa, mica, cable, soporte, repuesto) "
                    f"o un DISPOSITIVO PRINCIPAL (smartphone, notebook, consola, tablet).\n"
                    f"Importante: Si dice 'funda para iPhone 15', el producto es la funda (accesorio), "
                    f"el iPhone es solo el modelo compatible.\n\n"
                    f"Responde estrictamente en formato JSON con las siguientes llaves:\n"
                    f'{{"is_accessory": bool, "is_main_device": bool, "detected_item_type": str, '
                    f'"explanation": str, "confidence": float}}'
                )
                msg = client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw_text = msg.content[0].text
                # Extraer JSON de la respuesta
                json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group(0))
                    return SemanticClassificationResult(**data)
            except Exception:
                pass

        return None


# ------------------------------------------------------------------------------
# Pruebas de Verificación y Demostración Contrastiva en Runtime
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 80)
    print(" 🛡️  GLITCHHUNTER AI - MOTOR SEMÁNTICO Y FILTRO ANTI-ACCESORIOS  🛡️")
    print("=" * 80 + "\n")

    filter_engine = SemanticGlitchFilter()

    # Casos de prueba contrastivos exigidos por los requerimientos
    test_cases = [
        {
            "title": "iPhone 15 Pro Max 256GB Titanio Natural",
            "price": 119990.0,
            "normal_price": 1299990.0,
            "expected_verdict": "DISPOSITIVO PRINCIPAL (GLITCH CONFIRMADO)",
        },
        {
            "title": "Funda Silicona Magnética MagSafe compatible con iPhone 15 Pro Max",
            "price": 9990.0,
            "normal_price": 19990.0,
            "expected_verdict": "ACCESORIO (DESCARTADO / PRECIO NORMAL)",
        },
        {
            "title": "MacBook Pro 14 M3 512GB",
            "price": 249990.0,
            "normal_price": 1899990.0,
            "expected_verdict": "DISPOSITIVO PRINCIPAL (GLITCH CONFIRMADO)",
        },
        {
            "title": "Carcasa Rigida Transparente para MacBook Pro 14 M3",
            "price": 14990.0,
            "normal_price": 29990.0,
            "expected_verdict": "ACCESORIO (DESCARTADO)",
        },
        # Casos contrastivos complementarios para máxima rigurosidad
        {
            "title": "Samsung Galaxy S24 Ultra 512GB Titanium Black",
            "price": 149990.0,
            "normal_price": 1469990.0,
            "expected_verdict": "DISPOSITIVO PRINCIPAL (GLITCH CONFIRMADO)",
        },
        {
            "title": "Lámina Vidrio Templado 9D Full Cover para Samsung Galaxy S24 Ultra",
            "price": 4990.0,
            "normal_price": 12990.0,
            "expected_verdict": "ACCESORIO (DESCARTADO / PRECIO NORMAL)",
        },
    ]

    all_passed = True

    for i, test in enumerate(test_cases, 1):
        evaluation = filter_engine.evaluate_glitch_candidate(
            title=test["title"],
            current_price=test["price"],
            normal_price=test["normal_price"],
        )

        res = evaluation["semantic_result"]
        verdict = evaluation["verdict_summary"]

        # Comprobación de concordancia
        matched = (verdict == test["expected_verdict"]) or (
            "ACCESORIO" in verdict and "ACCESORIO" in test["expected_verdict"]
        ) and (
            "GLITCH CONFIRMADO" in verdict if "GLITCH CONFIRMADO" in test["expected_verdict"] else True
        )

        if not matched:
            all_passed = False

        status_icon = "✅" if matched else "❌"
        print(f"[{i}] {status_icon} Producto: \"{test['title']}\"")
        print(f"    • Precio Oferta: ${test['price']:,.0f} CLP | Normal: ${test['normal_price']:,.0f} CLP")
        print(f"    • Clasificación Semántica: Tipo={res.detected_item_type} | Confianza={res.confidence * 100:.1f}%")
        print(f"    • Es Accesorio: {res.is_accessory} | Es Dispositivo Principal: {res.is_main_device}")
        print(f"    • Veredicto Final: {verdict}")
        print(f"    • Explicación: {evaluation['explanation']}")
        print("-" * 80)

    print("\n" + "=" * 80)
    if all_passed:
        print(" 🎉 TODAS LAS PRUEBAS CONTRASTIVAS SUPERADAS CON ÉXITO (100% PRECISIÓN) 🎉")
    else:
        print(" ⚠️ ALGUNAS PRUEBAS NO COINCIDIERON CON EL RESULTADO ESPERADO.")
    print("=" * 80 + "\n")
