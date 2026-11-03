"""
Clasificación de categoría por título, compartida por scrapers y filtros de calidad.

Busca palabras completas (no subcadenas: "ram" no debe calzar con "Duramo" ni "Tramontina")
y cubre las categorías de la rotación del catálogo. Lo que no corresponde a ninguna queda
como "otros" (ropa, muebles, juguetes…), en vez de caer por defecto en "tecnologia".
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

OTHER_CATEGORY = "otros"
ACCESSORY_CATEGORY = "accesorios"

# Orden importa: accesorios primero ("funda para iphone" no es un smartphone),
# y equipos que contienen componentes antes que los componentes ("notebook con rtx 4060").
_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    (ACCESSORY_CATEGORY, (
        "funda", "carcasa", "case", "lamina", "mica", "vidrio templado", "cable", "adaptador",
        "correa", "protector", "cargador", "soporte", "caja vacia", "solo caja", "bolso",
        "mochila", "maletin", "base refrigerante", "cooler para notebook", "tinta", "toner",
        "cartucho", "repuesto",
    )),
    ("notebooks", ("notebook", "laptop", "macbook", "thinkpad", "zenbook", "ideapad", "vivobook", "chromebook")),
    ("smartphones", ("iphone", "celular", "smartphone", "galaxy s", "galaxy a", "galaxy z", "xiaomi", "redmi", "motorola", "pixel")),
    ("tablets", ("tablet", "ipad", "galaxy tab")),
    ("televisores", ("smart tv", "televisor", "oled", "qled", "uhd 4k", "4k uhd")),
    ("monitores", ("monitor",)),
    ("consolas", ("playstation", "ps5", "ps4", "xbox", "nintendo switch", "consola")),
    ("smartwatch", ("smartwatch", "reloj inteligente", "smartband", "apple watch", "galaxy watch", "garmin")),
    ("audio", ("audifonos", "auriculares", "airpods", "parlante", "soundbar", "barra de sonido", "headset")),
    ("componentes", ("tarjeta de video", "geforce", "rtx", "radeon", "ryzen", "procesador", "placa madre", "ssd", "memoria ram", "ram ddr4", "ram ddr5", "fuente de poder")),
    ("gaming", ("mouse", "teclado", "silla gamer", "volante")),
    ("linea_blanca", ("refrigerador", "lavadora", "secadora", "horno", "microondas", "cocina", "freezer", "lavavajillas", "aire acondicionado")),
]

_COMPILED = [
    (cat, re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\b"))
    for cat, keys in _RULES
]


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def classify_category(title: str) -> str:
    """Categoría del radar para un título, 'accesorios' u 'otros'."""
    t = _normalize(title)
    for cat, pattern in _COMPILED:
        if pattern.search(t):
            return cat
    return OTHER_CATEGORY
