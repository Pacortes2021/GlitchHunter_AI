"""
Extractor de Código de Modelo para Retail Chileno.
Permite identificar con alta precisión el modelo exacto del producto (televisores,
procesadores, tarjetas de video, consolas, smartphones y laptops) para evitar
comparaciones erróneas entre variantes de hardware o capacidades distintas.
"""

from __future__ import annotations

import re
from typing import Optional

# Patrones regex de alta especificidad para hardware tecnológico
MODEL_PATTERNS = [
    # 1. Televisores Samsung (ej: QN65S90C, UN55CU7000)
    re.compile(r"\b(QN\d{2}[A-Za-z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\b(UN\d{2}[A-Za-z0-9]+)\b", re.IGNORECASE),

    # 2. Televisores LG (ej: OLED55C3PSA, 65QNED80SRA, 55UR7800PSB)
    re.compile(r"\b(OLED\d{2}[A-Za-z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\b(\d{2}[A-Za-z]{2,4}\d{3,4}[A-Za-z0-9]*)\b", re.IGNORECASE),

    # 3. Tarjetas de Video NVIDIA / AMD
    re.compile(r"\b(RTX\s*\d{4}(?:\s*(?:Super|Ti|SUPER|TI))?)\b", re.IGNORECASE),
    re.compile(r"\b(RX\s*\d{4}(?:\s*XTX|\s*XT)?)\b", re.IGNORECASE),

    # 4. Procesadores AMD / Intel (solo válidos si el producto ES el procesador, ver HOST_DEVICE_KEYWORDS)
    #    Apple M1-M4 no se usa: no se vende suelto y aparece en MacBook, iPad, iMac y Mac mini.
    re.compile(r"\b(Ryzen\s*[3579]\s*\d{4}[A-Za-z]*(?:\s*3D)?)\b", re.IGNORECASE),
    re.compile(r"\b(i[3579]-?\d{4,5}[A-Za-z]*)\b", re.IGNORECASE),

    # 5. Audio Profesional / Cancelación de Ruido (Sony WH-1000XM5, WF-1000XM4)
    re.compile(r"\b([Ww][HhFf]-\d{3,4}[A-Za-z0-9]+)\b", re.IGNORECASE),

    # 6. Smartphones Samsung / Apple
    re.compile(r"\b(SM-[A-Za-z]\d{3}[A-Za-z0-9]*)\b", re.IGNORECASE),
    # iPhone solo junto a su capacidad (ej: "iPhone 15 Pro 256GB"): sin ella, 128GB y 1TB tendrían la misma mediana
    re.compile(r"\b(iPhone\s*1[1-6](?:\s*(?:Pro(?:\s*Max)?|Plus|Mini))?)\b(?:(?!\d+\s*(?:GB|TB)).){0,40}?(\d{2,4}\s*(?:GB|TB))", re.IGNORECASE),

    # 7. Consolas: "PlayStation 5" / "Xbox Series X" son nombres de línea, no de modelo (aparecen en
    #    controles, juegos y ediciones distintas), por lo que no se usan para comparar precios.

    # 8. Códigos de parte explícitos entre corchetes: [A0DH7LA], [QN65S90C], etc.
    re.compile(r"\[([A-Za-z0-9\-]{4,15})\]"),

    # 9. Monitores y Laptops específicas (ej: AW3423DWF, 14-FB0004LA)
    re.compile(r"\b([A-Za-z]{1,4}\d{2,4}[A-Za-z0-9\-]{2,10})\b", re.IGNORECASE),
]

IGNORED_TAG_KEYWORDS = {
    "pro", "max", "slim", "plus", "mini", "oled", "uhd", "test", "demo",
    "reacondicionado", "refurbished", "outlet", "oferta", "cyber", "cyberday",
    "nuevo", "pack", "combo", "promo", "envio", "gratis", "despacho",
    "original", "garantia", "cuotas", "sin interes", "open box", "segunda seleccion",
}

IGNORED_TAG_PREFIXES = ("test-", "demo-", "pack-", "promo-")

# Códigos de chip (GPU / CPU) que identifican un componente, no el producto completo
COMPONENT_CODE_RE = re.compile(r"^(rtx|gtx|rx|ryzen|i[3579])", re.IGNORECASE)

# Si el título describe un equipo que CONTIENE el chip, el código del chip no identifica el producto:
# un notebook con RTX 4060 no vale lo mismo que una tarjeta de video RTX 4060.
HOST_DEVICE_KEYWORDS = (
    "notebook", "laptop", "portatil", "portátil", "computador", "pc gamer", "pc gaming",
    "all in one", "all-in-one", "desktop", "torre", "macbook", "imac", "mac mini",
    "ipad", "tablet", "consola", "mini pc",
)


def normalize_model_code(code: str) -> str:
    """Forma canónica para comparar códigos: minúsculas, sin espacios ni guiones."""
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


def _is_host_device(title: str) -> bool:
    t = title.lower()
    return any(re.search(rf"\b{re.escape(k)}\b", t) for k in HOST_DEVICE_KEYWORDS)


def extract_model_code(title: str) -> Optional[str]:
    """
    Extrae el código o identificador de modelo estricto de un título de producto.
    Si el título no contiene un código de modelo reconocible, retorna None
    para evitar agrupaciones erróneas o aproximaciones imprecisas.
    """
    if not title or not isinstance(title, str):
        return None

    clean_title = title.strip()
    is_host = _is_host_device(clean_title)
    for pattern in MODEL_PATTERNS:
        for match in pattern.finditer(clean_title):
            code = match.group(1).strip()
            if match.lastindex and match.lastindex >= 2:
                # Patrones con capacidad (iPhone): "iPhone 15 Pro" + "256GB"
                capacity = re.sub(r"\s+", "", match.group(2)).upper()
                code = f"{code} {capacity}"
            lower = code.lower().strip("-")
            # Descartar si el código resultante es demasiado genérico, un tag de marketing o test
            if len(code) < 3 or lower in IGNORED_TAG_KEYWORDS:
                continue
            if any(lower.startswith(p) for p in IGNORED_TAG_PREFIXES):
                continue
            # Un chip dentro de un equipo (notebook, PC, consola) no identifica el equipo
            if is_host and COMPONENT_CODE_RE.match(normalize_model_code(code)):
                continue
            # Para códigos entre corchetes, exigir que tengan tanto letras como números
            # (evita falsos positivos como [REACONDICIONADO] o [OFERTA])
            if "[" in pattern.pattern:
                has_digit = any(c.isdigit() for c in code)
                has_alpha = any(c.isalpha() for c in code)
                if not (has_digit and has_alpha):
                    continue
            return code

    return None
