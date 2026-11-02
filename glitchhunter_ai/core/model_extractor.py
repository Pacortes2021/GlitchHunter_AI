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
    # 1. Códigos entre corchetes explícitos: [A0DH7LA], [QN65S90C], etc.
    re.compile(r"\[([A-Za-z0-9\-]{4,15})\]"),

    # 2. Televisores Samsung (ej: QN65S90C, UN55CU7000)
    re.compile(r"\b(QN\d{2}[A-Za-z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\b(UN\d{2}[A-Za-z0-9]+)\b", re.IGNORECASE),

    # 3. Televisores LG (ej: OLED55C3PSA, 65QNED80SRA, 55UR7800PSB)
    re.compile(r"\b(OLED\d{2}[A-Za-z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\b(\d{2}[A-Za-z]{2,4}\d{3,4}[A-Za-z0-9]*)\b", re.IGNORECASE),

    # 4. Tarjetas de Video NVIDIA / AMD
    re.compile(r"\b(RTX\s*\d{4}(?:\s*(?:Super|Ti|SUPER|TI))?)\b", re.IGNORECASE),
    re.compile(r"\b(RX\s*\d{4}(?:\s*XTX|\s*XT)?)\b", re.IGNORECASE),

    # 5. Procesadores AMD / Intel / Apple
    re.compile(r"\b(Ryzen\s*[3579]\s*\d{4}[A-Za-z]*(?:\s*3D)?)\b", re.IGNORECASE),
    re.compile(r"\b(i[3579]-?\d{4,5}[A-Za-z]*)\b", re.IGNORECASE),
    re.compile(r"\b(Apple\s*M[1-4](?:\s*(?:Pro|Max))?)\b", re.IGNORECASE),

    # 6. Audio Profesional / Cancelación de Ruido (Sony WH-1000XM5, WF-1000XM4)
    re.compile(r"\b([Ww][HhFf]-\d{3,4}[A-Za-z0-9]+)\b", re.IGNORECASE),

    # 7. Smartphones Samsung / Apple
    re.compile(r"\b(SM-[A-Za-z]\d{3}[A-Za-z0-9]*)\b", re.IGNORECASE),
    re.compile(r"\b(iPhone\s*1[1-6](?:\s*(?:Pro(?:\s*Max)?|Plus|Mini))?)\b", re.IGNORECASE),

    # 8. Consolas de Videojuegos
    re.compile(r"\b(PlayStation\s*5(?:\s*Slim)?|Xbox\s*Series\s*[XS]|Nintendo\s*Switch(?:\s*OLED)?)\b", re.IGNORECASE),

    # 9. Monitores y Laptops específicas (ej: AW3423DWF, 14-FB0004LA)
    re.compile(r"\b([A-Za-z]{1,4}\d{2,4}[A-Za-z0-9\-]{2,10})\b", re.IGNORECASE),
]


def extract_model_code(title: str) -> Optional[str]:
    """
    Extrae el código o identificador de modelo estricto de un título de producto.
    Si el título no contiene un código de modelo reconocible, retorna None
    para evitar agrupaciones erróneas o aproximaciones imprecisas.
    """
    if not title or not isinstance(title, str):
        return None

    clean_title = title.strip()
    for pattern in MODEL_PATTERNS:
        match = pattern.search(clean_title)
        if match:
            code = match.group(1).strip()
            # Descartar si el código resultante es demasiado genérico o corto
            if len(code) >= 3 and not code.lower() in ("pro", "max", "slim", "plus", "mini", "oled", "uhd"):
                return code

    return None
