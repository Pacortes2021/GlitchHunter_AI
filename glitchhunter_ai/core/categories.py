"""
Clasificación de categoría por título, compartida por scrapers y filtros de calidad.

Busca palabras completas (no subcadenas: "ram" no debe calzar con "Duramo" ni "Tramontina")
y cubre las categorías de la rotación del catálogo y los rubros de las tiendas de marca (ropa,
belleza, deportes, herramientas, hogar). Lo que no corresponde a ninguna queda como "otros",
en vez de caer por defecto en "tecnologia".
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
    ("audio", ("audifonos", "auriculares", "airpods", "parlante", "soundbar", "barra de sonido", "headset", "microfono")),
    ("componentes", ("tarjeta de video", "geforce", "rtx", "radeon", "ryzen", "procesador", "placa madre", "ssd", "memoria ram", "ram ddr4", "ram ddr5", "fuente de poder")),
    ("gaming", ("mouse", "teclado", "silla gamer", "volante")),
    ("linea_blanca", ("refrigerador", "lavadora", "secadora", "horno", "microondas", "cocina", "freezer", "lavavajillas", "aire acondicionado", "encimera", "campana", "calefont", "termo electrico")),
    # Rubros de las tiendas de marca, después de tecnología ("audífonos deportivos" sigue siendo audio)
    ("ropa_calzado", (
        "zapatilla", "zapatillas", "zapato", "zapatos", "bota", "botas", "botin", "botines", "sandalia",
        "sandalias", "chala", "chalas", "zueco", "zuecos", "clog", "clogs", "polera", "poleras", "camisa",
        "blusa", "chaqueta", "parka", "cortaviento", "chaleco", "pantalon", "pantalones", "jeans", "jean",
        "buzo", "poleron", "vestido", "falda", "short", "shorts", "bermuda", "calza", "calzas", "calcetin",
        "calcetines", "pijama", "traje de bano", "gorro", "jockey", "blazer", "sweater", "abrigo",
    )),
    ("belleza", (
        "perfume", "eau de parfum", "eau de toilette", "edp", "edt", "colonia", "body splash", "crema",
        "serum", "maquillaje", "labial", "shampoo", "acondicionador", "secador de pelo", "alisador",
        "afeitadora", "rasuradora", "depiladora", "esmalte", "highlighter", "iluminador", "rubor", "delineador",
    )),
    ("deportes", (
        "bicicleta", "bicicletas", "bici", "trotadora", "mancuerna", "mancuernas", "pesas", "carpa",
        "saco de dormir", "casco", "scooter", "patineta", "kayak", "balon", "jersey", "guantes", "rodillera",
        "pedal", "pedales", "ciclo computador", "camping",
    )),
    ("herramientas", (
        "taladro", "atornillador", "rotomartillo", "sierra", "esmeril", "compresor", "hidrolavadora",
        "generador", "herramienta", "herramientas",
    )),
    ("hogar", (
        "colchon", "sabana", "sabanas", "almohada", "almohadas", "plumon", "cobertor", "toalla", "toallas",
        "sofa", "silla", "mesa", "escritorio", "lampara", "olla", "ollas", "sarten", "bateria de cocina",
        "cafetera", "hervidor", "licuadora", "batidora", "tostadora", "freidora", "aspiradora", "plancha",
        "ventilador", "estufa", "calefactor", "purificador", "cuchillo", "cuchillos", "escobilla", "wok",
        "bowl", "bowls", "contenedor", "contenedores", "selladora", "multiolla", "comedor", "vajilla",
    )),
]

_COMPILED = [
    (cat, re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\b"))
    for cat, keys in _RULES
]


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# Rubros fuera de la rotación de tecnología del radar (ver DEAL_INCLUDE_NON_TECH)
NON_TECH_CATEGORIES = {OTHER_CATEGORY, "ropa_calzado", "belleza", "deportes", "herramientas", "hogar"}
# Rótulos de scrapers que no dicen nada del producto: se reemplazan por la clasificación del título
GENERIC_CATEGORIES = {"", OTHER_CATEGORY, "general", "tecnologia", "computacion"}


def classify_category(title: str) -> str:
    """Categoría del radar para un título, un rubro de NON_TECH_CATEGORIES, 'accesorios' u 'otros'."""
    t = _normalize(title)
    for cat, pattern in _COMPILED:
        if pattern.search(t):
            return cat
    return OTHER_CATEGORY
