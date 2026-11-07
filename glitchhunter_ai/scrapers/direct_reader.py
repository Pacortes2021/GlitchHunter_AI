"""
Lectura Directa de Tiendas (Falabella y Ripley) para el Modo Híbrido.

Los agregadores (SoloTodo / Knasta) cubren todo el catálogo, pero sus precios llegan con
~40 minutos de retraso. Este lector consulta directamente las páginas de la tienda
(datos Next.js en __NEXT_DATA__) para:
  1. Escanear con mayor frecuencia lo prioritario (términos de la watchlist y categorías elegidas).
  2. Confirmar en tiempo real un candidato detectado vía agregador antes de alertar.

Incluye un freno automático por tienda: ante 403/429/5xx, una verificación anti-bot o una
página sin datos, la lectura directa de esa tienda se pausa con espera exponencial en vez de insistir.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from glitchhunter_ai.core.categories import OTHER_CATEGORY, classify_category
from glitchhunter_ai.core.models import ProductItem
from glitchhunter_ai.scrapers.product_ids import canonical_product_id, ripley_id_from_parent
from glitchhunter_ai.scrapers.stealth_client import default_stealth_client

logger = logging.getLogger("glitchhunter.scrapers.direct_reader")

# Tiendas VTEX: su API pública de catálogo (/api/catalog_system/pub/products/search) entrega precio,
# precio normal y stock, sin autenticación. Se recorre el catálogo completo por tramos, como PC Factory.
# id -> (nombre, sitio, categoría por defecto)
VTEX_STORES: Dict[str, tuple] = {
    "sony": ("Sony Store", "https://store.sony.cl", "tecnologia"),
    "motorola": ("Motorola", "https://www.motorola.cl", "smartphones"),
    "electrolux": ("Electrolux", "https://www.electrolux.cl", "linea_blanca"),
    "fensa": ("Fensa", "https://www.tiendafensa.cl", "linea_blanca"),
    "oster": ("Oster", "https://www.oster.cl", "linea_blanca"),
    "casaroyal": ("Casa Royal", "https://www.casaroyal.cl", "tecnologia"),
    "nike": ("Nike", "https://www.nike.cl", "otros"),
    "levis": ("Levi's", "https://www.levi.cl", "otros"),
    "reebok": ("Reebok", "https://www.reebok.cl", "otros"),
    "mimbral": ("Mimbral", "https://www.mimbral.cl", "otros"),
    "audiomusica": ("Audiomúsica", "https://www.audiomusica.com", "audio"),
    "dimeiggs": ("Dimeiggs", "https://www.dimeiggs.cl", "otros"),
    "drsimi": ("Dr. Simi", "https://www.drsimi.cl", "otros"),
    "mademsa": ("Mademsa", "https://www.tiendamademsa.cl", "linea_blanca"),
    "dimarsa": ("Dimarsa", "https://www.dimarsa.cl", "otros"),
    "marsella": ("Ferretería Marsella", "https://www.ferreteriamarsella.cl", "otros"),
    "construplaza": ("Construplaza", "https://www.construplaza.cl", "otros"),
    "tiendasalfa": ("Tiendas Alfa", "https://www.tiendasalfa.cl", "otros"),
    "tramontina": ("Tramontina", "https://www.tramontina.cl", "otros"),
    "dimarine": ("Dimarine", "https://www.dimarine.cl", "otros"),
    "lavinoteca": ("La Vinoteca", "https://www.lavinoteca.cl", "otros"),
    "maxservice": ("Maxservice", "https://www.maxservice.cl", "otros"),
    "ventus": ("Ventus", "https://www.ventuscorp.cl", "otros"),
    "karcher": ("Kärcher", "https://www.karcherstore.cl", "otros"),
    "esika": ("Ésika", "https://esika.tiendabelcorp.cl", "otros"),
    "lbel": ("L'Bel", "https://lbel.tiendabelcorp.cl", "otros"),
    "clubsoftys": ("Club Softys", "https://www.clubsoftys.cl", "otros"),
    "coleman": ("Coleman", "https://www.coleman.cl", "otros"),
    "marleycoffee": ("Marley Coffee", "https://www.marleycoffee.cl", "otros"),
    "bbqgrill": ("BBQ Grill", "https://www.bbqgrill.cl", "otros"),
    "blanik": ("Blanik", "https://www.blanik.cl", "otros"),
    "cotidian": ("Cotidian", "https://www.mitiendacotidian.cl", "otros"),
    "colloky": ("Colloky", "https://www.colloky.cl", "otros"),
    "ficcus": ("Ficcus", "https://www.ficcus.cl", "otros"),
    "zapatos": ("Zapatos.cl", "https://www.zapatos.cl", "otros"),
    "hm": ("H&M", "https://cl.hm.com", "otros"),
    "dolly": ("Dolly", "https://www.dolly.cl", "otros"),
    "ferouch": ("Ferouch", "https://www.ferouch.cl", "otros"),
    "calvinklein": ("Calvin Klein", "https://www.calvinklein.cl", "otros"),
    "theline": ("The Line", "https://www.theline.cl", "otros"),
    "tommy": ("Tommy Hilfiger", "https://cl.tommy.com", "otros"),
    "treck": ("Treck", "https://www.treck.cl", "otros"),
    "velez": ("Vélez", "https://www.velez.cl", "otros"),
    "privilege": ("Privilege", "https://www.privilege.cl", "otros"),
    "newman": ("Newman", "https://www.newman.cl", "otros"),
    "swarovski": ("Swarovski", "https://www.swarovski.cl", "otros"),
    "womensecret": ("Women'secret", "https://www.womensecret.cl", "otros"),
    "totto": ("Totto", "https://cl.totto.com", "otros"),
    "oneill": ("O'Neill", "https://www.oneill.cl", "otros"),
    "americaneagle": ("American Eagle", "https://www.ae.cl", "otros"),
    "asics": ("Asics", "https://www.asics.cl", "otros"),
    "trial": ("Trial", "https://www.trial.cl", "otros"),
    "umbrale": ("Umbrale", "https://www.umbrale.cl", "otros"),
    "mountainhardwear": ("Mountain Hardwear", "https://www.mountainhardwear.cl", "otros"),
    "ilgioco": ("Il Gioco", "https://www.ilgioco.cl", "otros"),
    "jansport": ("JanSport", "https://www.jansport.cl", "otros"),
    "lindt": ("Lindt", "https://www.lindt.cl", "otros"),
    "chilemat": ("Chilemat", "https://www.chilemat.com", "otros"),
    "palmers": ("Palmers", "https://www.palmers.cl", "otros"),
    "blushbar": ("Blush-Bar", "https://www.blush-bar.cl", "otros"),
    "mk": ("MK", "https://www.mk.cl", "otros"),
    "fernapet": ("Fernapet", "https://www.fernapet.cl", "otros"),
    "perryellis": ("Perry Ellis", "https://www.perryellis.cl", "otros"),
    "hit": ("Hit", "https://www.hit.cl", "otros"),
}

# Tiendas Shopify: /products.json es el catálogo público estándar de Shopify (precio, compare_at_price
# y disponibilidad por variante). Shopify frena por IP en TODAS sus tiendas a la vez (tras ~6 peticiones
# seguidas responde 403), así que se hace una sola petición Shopify cada SHOPIFY_SPACING_SECONDS,
# rotando entre tiendas, y un 403 pausa a todas (clave de pausa compartida "shopify").
# id -> (nombre, sitio, categoría por defecto)
SHOPIFY_STORES: Dict[str, tuple] = {
    "columbia": ("Columbia", "https://www.columbiachile.cl", "otros"),
    "caterpillar": ("Caterpillar", "https://www.shopcaterpillar.cl", "otros"),
    "patagonia": ("Patagonia", "https://cl.patagonia.com", "otros"),
    "underarmour": ("Under Armour", "https://www.underarmour.cl", "otros"),
    "crocs": ("Crocs", "https://www.crocs.cl", "otros"),
    "vans": ("Vans", "https://www.vans.cl", "otros"),
    "doite": ("Doite", "https://www.doite.cl", "otros"),
    "havaianas": ("Havaianas", "https://www.havaianas.cl", "otros"),
    "oxford": ("Oxford", "https://www.oxfordstore.cl", "otros"),
    "aufbau": ("Aufbau", "https://aufbau.cl", "tecnologia"),
    "lippi": ("Lippi", "https://www.lippioutdoor.com", "otros"),
    "kitchencenter": ("Kitchen Center", "https://www.kitchencenter.cl", "otros"),
    "sharkninja": ("SharkNinja", "https://www.sharkninja.cl", "otros"),
    "dermotienda": ("Dermotienda", "https://dermotienda.cl", "otros"),
    "herramientastotal": ("Herramientas Total", "https://www.herramientastotal.cl", "otros"),
    "delonghi": ("De'Longhi", "https://www.delonghi.cl", "otros"),
    "santarita": ("Santa Rita", "https://www.santaritaonline.com", "otros"),
    "petplanet": ("Pet Planet", "https://www.petplanet.cl", "otros"),
    "toyng": ("Toyng", "https://www.toyng.cl", "otros"),
    "juanvaldez": ("Juan Valdez", "https://www.juanvaldez.cl", "otros"),
    "promusic": ("ProMusic", "https://www.promusic.cl", "otros"),
    "stevemadden": ("Steve Madden", "https://stevemadden.cl", "otros"),
    "tiendacopec": ("Tienda Copec", "https://www.tiendacopec.cl", "otros"),
    "raindoor": ("Raindoor", "https://www.raindoor.cl", "otros"),
    "ansaldo": ("Ansaldo", "https://ansaldo.cl", "otros"),
    "adagio": ("Adagio Teas", "https://adagio.cl", "otros"),
    "a3d": ("A3D", "https://www.a3d.cl", "otros"),
    "zola": ("Zola", "https://zola.cl", "otros"),
    "hushpuppieskids": ("Hush Puppies Kids", "https://www.hushpuppieskids.cl", "otros"),
    "portomenaje": ("Porto Menaje", "https://www.portomenaje.cl", "otros"),
    "hollyconcept": ("Holly Concept", "https://hollyconcept.com", "otros"),
    "melissa": ("Melissa", "https://melissaoficial.cl", "otros"),
    "bamers": ("Bamers", "https://www.bamers.cl", "otros"),
    "doce34": ("Doce34", "https://doce34.cl", "otros"),
    "vantek": ("Vantek", "https://vantek.cl", "otros"),
    "yeppo": ("Yeppo", "https://yeppo.cl", "otros"),
    "mashini": ("Mashini", "https://mashini.cl", "otros"),
    "zooyork": ("Zoo York", "https://www.zooyork.cl", "otros"),
    "blikstore": ("Blik Store", "https://www.blikstore.com", "otros"),
    "fashionspark": ("Fashion's Park", "https://www.fashionspark.com", "otros"),
}
# Shopify con compare_at_price inflado: Kitchen Center muestra "antes" de exactamente el doble
# ($409.980 = 2 × $204.990). Ahí la referencia es el precio actual, como Casa Royal en VTEX.
SHOPIFY_INFLATED_COMPARE_PRICE = {"kitchencenter"}
# Tiendas Magento 2: el mismo HTML de listado que HP (li.product-item, data-price-amount con
# finalPrice/oldPrice). Se recorren sus secciones por páginas de 36, rotando en cada ciclo.
# id -> (nombre, sitio, categoría por defecto, secciones)
MAGENTO_STORES: Dict[str, tuple] = {
    "thenorthface": ("The North Face", "https://www.thenorthface.cl", "otros", ("/hombre", "/mujer")),
    "converse": ("Converse", "https://www.converse.cl", "otros", ("/hombre", "/mujer")),
    "lacoste": ("Lacoste", "https://www.lacoste.cl", "otros", ("/hombre", "/mujer")),
    "marmot": ("Marmot", "https://www.marmot.cl", "otros", ("/hombre", "/mujer")),
    "sparta": ("Sparta", "https://sparta.cl", "otros", ("/zapatillas.html",)),
    "dbs": ("DBS", "https://dbs.cl", "otros", ("/sale", "/perfumes/mujer", "/perfumes/hombre")),
    "antartica": ("Antártica", "https://www.antartica.cl", "otros", ("/promociones.html", "/libros-importados.html", "/libros.html")),
    "weplay": ("Weplay", "https://www.weplay.cl", "consolas", ("/oferta.html", "/consolas.html")),
    "sdn": ("SDN Neumáticos", "https://sdn.cl", "otros", ("/ofertas.html", "/neumaticos.html")),
    "novasalud": ("Novasalud", "https://www.novasalud.cl", "otros", ("/ofertas", "/medicamentos", "/cuidado-personal")),
    "ebest": ("eBest", "https://www.ebest.cl", "otros", ("/fotografia-video.html", "/audio.html", "/bicicleta.html")),
    "bathandbodyworks": ("Bath & Body Works", "https://www.bathandbodyworks.cl", "otros", ("/catalogo",)),
    "schilling": ("Schilling", "https://www.schilling.cl", "otros", ("/lentes-de-sol.html", "/lentes-opticos.html")),
    "flex": ("Flex", "https://www.flex.cl", "otros", ("/ofertas", "/camas", "/colchones")),
    "headchile": ("Head", "https://www.headchile.com", "otros", ("/mochilas", "/maletas")),
    "epson": ("Tienda Epson", "https://www.tiendaepson.cl", "otros", ("/ofertas", "/impresoras", "/proyectores")),
    "umbro": ("Umbro", "https://umbro.cl", "otros", ("/outlet", "/hombre")),
    "kotting": ("Kotting", "https://www.kotting.cl", "otros", ("/sale.html", "/vestuario.html")),
    "roly": ("Roly", "https://www.roly.cl", "otros", ("/hombre.html", "/mujer.html")),
    "surprice": ("Surprice", "https://www.surprice.cl", "otros", ("/ofertas", "/hombre", "/mujer")),
    "ash": ("Ash", "https://www.ashchile.cl", "otros", ("/women.html",)),
    "bananarepublic": ("Banana Republic", "https://www.bananarepublic.cl", "otros", ("/sale", "/mujer-cat", "/hombre-cat")),
    "brooksbrothers": ("Brooks Brothers", "https://www.brooksbrothers.cl", "otros", ("/hombre", "/mujer")),
    "calubags": ("Calu Bags", "https://www.calubags.com", "otros", ("/todo-calu",)),
    "dcshoes": ("DC Shoes", "https://www.dcshoes.cl", "otros", ("/hombre", "/mujer")),
    "globe": ("Globe", "https://globebrand.cl", "otros", ("/footwear.html",)),
    "guess": ("Guess", "https://www.guess.cl", "otros", ("/mujer", "/carteras", "/hombre")),
    "jayson": ("Jayson", "https://www.jayson.cl", "otros", ("/sale.html", "/institucional.html")),
    "kannu": ("Kannu", "https://www.kannu.cl", "otros", ("/sale.html", "/kannu-mujer.html", "/hombre.html")),
    "kipling": ("Kipling", "https://www.kipling.cl", "otros", ("/carteras", "/mochilas", "/accesorios")),
    "magriffe": ("Magriffe", "https://www.magriffe.cl", "otros", ("/outlet.html", "/oportunidades.html")),
    "mammut": ("Mammut", "https://www.mammut.cl", "otros", ("/sale", "/hombre", "/mujer", "/equipamiento")),
    "mauiandsons": ("Maui and Sons", "https://mauiandsons.cl", "otros", ("/hombre.html", "/mujer.html", "/kids.html")),
    "mcgregor": ("McGregor", "https://www.mcgregor.cl", "otros", ("/sale.html", "/casual.html")),
    "potros": ("Potros", "https://www.potros.cl", "otros", ("/sale.html", "/vestuario.html")),
    "rapsodia": ("Rapsodia", "https://www.rapsodia.cl", "otros", ("/special-prices.html", "/woman.html")),
    "ripcurl": ("Rip Curl", "https://ripcurl.cl", "otros", ("/hombre.html", "/mujer.html", "/juvenil.html")),
    "rusty": ("Rusty", "https://rusty.cl", "otros", ("/sale.html", "/hombre.html")),
    "stoked": ("Stoked", "https://www.stoked.cl", "otros", ("/hombre", "/mujer")),
    "gap": ("Gap", "https://www.gap.cl", "otros", ("/mujer", "/hombre", "/ni-os")),
    "ugg": ("UGG", "https://www.ugg.cl", "otros", ("/sale", "/mujer", "/hombre")),
    "victoriassecret": ("Victoria's Secret", "https://www.victoriassecret.cl", "otros", ("/bras", "/panties", "/beauty")),
    "volcom": ("Volcom", "https://volcom.cl", "otros", ("/hombre.html", "/footwear.html")),
    "farmaciamapuche": ("Farmacia Mapuche", "https://farmaciamapuche.cl", "otros", ("/ofertas.html", "/suplementos-y-alimentos", "/cosmetica-natural-y-belleza", "/medicamentos-naturales")),
    "rosen": ("Rosen", "https://www.rosen.cl", "otros", ("/camas-y-colchones.html", "/muebles.html", "/ropa-de-cama.html")),
    "cannonhome": ("Cannon Home", "https://cannonhome.cl", "otros", ("/12-ropa-de-cama", "/plaza", "/dormitorio", "/bano")),
    "wa2": ("Wa2", "https://www.wa2.cl", "otros", ("/colecciones",)),
    "coliseum": ("Coliseum", "https://coliseumstore.cl", "otros", ('/sale', '/todo-ropa', '/hombre/calzado', '/mujer/zapatillas')),
    "todomoda": ("Todo Moda", "https://cl.todomoda.com", "otros", ('/sale.html', '/accesorios.html')),
    "kliper": ("Kliper", "https://www.kliper.cl", "otros", ('/ofertas', '/hombre', '/mujer')),
}
MAGENTO_PAGE_SIZE = 36
MAGENTO_PAGES_PER_CYCLE = 2

SHOPIFY_PAGE_SIZE = 250          # máximo de /products.json
SHOPIFY_SPACING_SECONDS = 90.0   # una petición Shopify (de cualquier tienda) cada 90 s
SHOPIFY_BACKOFF_KEY = "shopify"
# Tiendas cuyo ListPrice está inflado en todo el catálogo (como el "normal" de ABC): Casa Royal
# muestra 40%+ de descuento en ~18% de sus productos (un micrófono de $18.990 con "normal" $54.990);
# Construplaza, Coleman e Il Gioco muestran "descuento" en casi el 100% del catálogo.
# Ahí la referencia es el precio actual y las bajas reales se detectan contra el historial propio.
VTEX_INFLATED_LIST_PRICE = {"casaroyal", "construplaza", "coleman", "ilgioco", "jansport", "chilemat", "fernapet"}
VTEX_PAGE_SIZE = 50          # máximo que acepta la API
VTEX_MAX_OFFSET = 2500       # VTEX no pagina más allá de _from=2500
VTEX_PAGES_PER_CYCLE = 4     # 200 productos por tienda y ciclo
# Carril rápido: en cada ciclo, además del recorrido completo (lento, para el historial), se leen los
# productos con mayor descuento de todo el catálogo (VTEX lo ordena) o la primera página de la sección
# de ofertas (Magento, Santa Isabel). Así un descuento grande aparece en minutos, no en horas.
VTEX_HOT_ORDER = "OrderByBestDiscountDESC"

DIRECT_STORES = (
    ("falabella", "ripley", "paris", "abc", "jumbo", "santaisabel", "hp", "winpy", "sodimac", "pcfactory", "spdigital")
    + tuple(VTEX_STORES) + tuple(SHOPIFY_STORES) + tuple(MAGENTO_STORES)
)

# Secciones de ofertas: el carril rápido de una tienda Magento relee su primera sección solo si es una de estas
_SALE_SECTION_RE = re.compile(r"sale|oferta|outlet|promo|special|oportunidad|liquidac|descuento|rebaja", re.IGNORECASE)
# Colecciones Shopify de ofertas: primero los nombres exactos, luego cualquier coincidencia
_SHOPIFY_SALE_EXACT = ("sale", "ofertas", "outlet", "descuentos", "liquidacion", "rebajas", "promociones", "oferta")
_SHOPIFY_SALE_RE = re.compile(r"sale|oferta|outlet|descuento|liquidac|rebaja|promo|cyber|black", re.IGNORECASE)


def supports_live_search(store_id: str) -> bool:
    """Tiendas cuyo buscador propio se puede consultar (DirectStoreReader.search devuelve resultados)."""
    return (store_id in VTEX_STORES or store_id in MAGENTO_STORES
            or store_id in ("falabella", "ripley", "paris", "abc", "sodimac", "pcfactory"))


def magento_has_sale_section(store_id: str) -> bool:
    return bool(_SALE_SECTION_RE.search(MAGENTO_STORES[store_id][3][0]))


def pick_shopify_sale_collection(collections: List[Dict[str, Any]]) -> Optional[str]:
    """Handle de la colección de ofertas de una tienda Shopify (de /collections.json), o None."""
    candidates = []
    for c in collections:
        handle = str(c.get("handle") or "")
        text = f"{handle} {c.get('title') or ''}"
        if not handle or not _SHOPIFY_SALE_RE.search(text):
            continue
        count = c.get("products_count")
        if count == 0:
            continue
        exact = handle.lower() in _SHOPIFY_SALE_EXACT
        candidates.append((not exact, -(count or 0), handle))
    return min(candidates)[2] if candidates else None


# Tiendas con carril rápido (una petición a sus ofertas en cada turno corto)
HOT_LANE_STORES = (
    tuple(VTEX_STORES) + tuple(s for s in MAGENTO_STORES if magento_has_sale_section(s)) + ("santaisabel",)
)
# Tiendas con recorrido de catálogo propio (las demás solo leen la watchlist y las categorías)
CATALOG_STORES = (
    ("santaisabel", "pcfactory", "spdigital") + tuple(VTEX_STORES) + tuple(MAGENTO_STORES)
)

# PC Factory expone una API JSON pública (la que usa su propio sitio): búsqueda, catálogo completo
# paginado (search=*) y detalle por id. El catálogo (~3.000 productos, 48 por página) se recorre
# por partes en cada ciclo para no saturar su API: con 10 páginas por ciclo de 3 min, una vuelta
# completa toma ~20 min.
PCFACTORY_API = "https://api.pcfactory.cl/pcfactory-services-catalogo/v1/catalogo/productos"
PCFACTORY_HEADERS = {"Origin": "https://www.pcfactory.cl", "Referer": "https://www.pcfactory.cl/"}
PCFACTORY_PAGE_SIZE = 48

# SP Digital (Gatsby): los listados /categories/{sección}/{página}/ traen 48 tarjetas con precio normal,
# precio por transferencia y precio con otros medios de pago. Se recorren por secciones, como Magento.
SPDIGITAL_SECTIONS = ("/categories/componentes/", "/categories/computacion/", "/categories/gaming-y-streaming/",
                      "/categories/audio-y-musica/", "/categories/conectividad-y-redes/", "/categories/hogar-y-oficina/",
                      "/categories/otras-categorias/")
SPDIGITAL_PAGES_PER_CYCLE = 3
# Santa Isabel (Cencosud, mismo sitio que Jumbo): secciones del supermercado con ?page=N; pasada la
# última página repite la primera, así que la sección termina cuando una página no trae productos nuevos.
SANTAISABEL_SECTIONS = ("/ofertas", "/botilleria", "/despensa", "/limpieza", "/cuidado-personal-y-bebe",
                        "/hogar-jugueteria-y-libreria", "/chocolates-galletas-y-snacks", "/lacteos-huevos-y-congelados",
                        "/mascotas", "/carnes-y-pescados", "/quesos-y-fiambres", "/productos-importados")
SANTAISABEL_PAGES_PER_CYCLE = 3
SANTAISABEL_MAX_PAGES = 15

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
_BOT_CHALLENGE_MARKERS = ("captcha", "perimeterx", "px-captcha", "are you a robot", "access denied")

STORE_NAMES = {
    "falabella": "Falabella", "ripley": "Ripley", "paris": "Paris", "abc": "ABC",
    "jumbo": "Jumbo", "hp": "HP", "winpy": "Winpy", "sodimac": "Sodimac", "pcfactory": "PC Factory",
    "spdigital": "SP Digital", "santaisabel": "Santa Isabel",
    **{sid: cfg[0] for sid, cfg in VTEX_STORES.items()},
    **{sid: cfg[0] for sid, cfg in SHOPIFY_STORES.items()},
    **{sid: cfg[0] for sid, cfg in MAGENTO_STORES.items()},
}
STORE_BASE = {
    "falabella": "https://www.falabella.com",
    "ripley": "https://simple.ripley.cl",
    "paris": "https://www.paris.cl",
    "abc": "https://www.abc.cl",
    "jumbo": "https://www.jumbo.cl",
    "hp": "https://www.hp.com",
    "winpy": "https://www.winpy.cl",
    **{sid: cfg[1] for sid, cfg in VTEX_STORES.items()},
    **{sid: cfg[1] for sid, cfg in SHOPIFY_STORES.items()},
    **{sid: cfg[1] for sid, cfg in MAGENTO_STORES.items()},
    "sodimac": "https://www.sodimac.cl",
    "pcfactory": "https://www.pcfactory.cl",
    "spdigital": "https://www.spdigital.cl",
    "santaisabel": "https://www.santaisabel.cl",
}

# Categorías verificadas para lectura directa (clave -> ruta)
CATEGORY_PATHS: Dict[str, Dict[str, str]] = {
    "falabella": {
        "tecnologia": "/falabella-cl/category/cat20002/Tecnologia",
        "smartphones": "/falabella-cl/category/cat70008/Smartphones-y-Telefonia",
        "notebooks": "/falabella-cl/category/cat2018/Computacion",
        "consolas": "/falabella-cl/category/cat2012/Consolas-y-Videojuegos",
        "televisores": "/falabella-cl/category/cat2070/Televisores",
    },
    # Sodimac: sus rutas de categoría redirigen a la portada; la búsqueda sí funciona (y a veces
    # redirige a una página /lista/ con el formato de Falabella, que el parser también entiende).
    "sodimac": {
        "televisores": "/sodimac-cl/lista/cat7190148/Smart-TV",
        "linea_blanca": "/sodimac-cl/search?Ntt=refrigerador",
        "lavadoras": "/sodimac-cl/search?Ntt=lavadora",
        "herramientas": "/sodimac-cl/search?Ntt=taladro",
    },
    # Paris: Next.js App Router; cada página trae ~30 productos (ignora ?page=). Línea blanca vía búsqueda.
    "paris": {
        "smartphones": "/tecnologia/celulares/",
        "notebooks": "/tecnologia/computadores/notebooks/",
        "televisores": "/electro/television/",
        "consolas": "/tecnologia/consolas-videojuegos/",
        "linea_blanca": "/search?q=refrigerador",
    },
    # ABC (Salesforce Commerce Cloud): ?sz=48 trae 48 productos por página; notebooks vía búsqueda
    # (la búsqueda "consola" devuelve sofás "consola", así que no se incluye)
    "abc": {
        "televisores": "/tecnologia/televisores/?sz=48",
        "smartphones": "/tecnologia/celulares/?sz=48",
        "linea_blanca": "/linea-blanca/refrigeradores/?sz=48",
        "notebooks": "/on/demandware.store/Sites-Abc-Site/es_CL/Search-Show?q=notebook&sz=48",
    },
    # Jumbo (Cencosud; Next.js App Router): solo vende electro pequeño y accesorios de tecnología.
    # Su búsqueda responde 404 a peticiones directas, así que se leen estas dos categorías (~50 por página).
    "jumbo": {
        "linea_blanca": "/hogar-electro-y-libreria/electro-y-tecnologia/electrodomesticos",
        "notebooks": "/hogar-electro-y-libreria/electro-y-tecnologia/tecnologia",
    },
    # HP Store (Magento): hasta 36 notebooks por página (el catálogo tiene ~55)
    "hp": {
        "notebooks": "/cl-es/shop/notebooks.html?product_list_limit=36",
    },
    # Winpy: HTML propio, ~40 productos por página
    "winpy": {
        "notebooks": "/portatiles/notebooks/",
        "smartphones": "/portatiles/celulares/",
    },
    "ripley": {
        "smartphones": "/tecno/celulares",
        "notebooks": "/tecno/computacion/notebooks",
        "televisores": "/tecno/television",
        "consolas": "/tecno/mundo-gamer",
    },
}


class BlockedError(Exception):
    """La tienda respondió con un bloqueo o una página sin datos utilizables."""


class StoreBackoff:
    """Pausa con espera exponencial por tienda tras un bloqueo (10 min, 20, 40… hasta 2 h)."""

    def __init__(self, base_seconds: float = 600.0, max_seconds: float = 7200.0, clock: Callable[[], float] = time.monotonic):
        self.base = base_seconds
        self.max = max_seconds
        self.clock = clock
        self._failures: Dict[str, int] = {}
        self._paused_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_paused(self, store_id: str) -> bool:
        with self._lock:
            return self.clock() < self._paused_until.get(store_id, 0.0)

    def remaining_seconds(self, store_id: str) -> float:
        with self._lock:
            return max(0.0, self._paused_until.get(store_id, 0.0) - self.clock())

    def record_block(self, store_id: str, reason: str) -> float:
        with self._lock:
            n = self._failures.get(store_id, 0) + 1
            self._failures[store_id] = n
            wait = min(self.max, self.base * (2 ** (n - 1)))
            self._paused_until[store_id] = self.clock() + wait
        logger.warning("⛔ [Lectura directa] %s bloqueó o no entregó datos (%s). Pausa de %.0f min.", store_id, reason, wait / 60)
        return wait

    def record_success(self, store_id: str) -> None:
        with self._lock:
            self._failures.pop(store_id, None)
            self._paused_until.pop(store_id, None)


def _extract_next_data(html: str) -> Dict[str, Any]:
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        raise BlockedError("página sin __NEXT_DATA__")
    return json.loads(m.group(1))


def _clean_price(val: Any) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, list):
        val = val[0] if val else ""
    digits = re.sub(r"[^\d]", "", str(val))
    return float(digits) if digits else 0.0


def _falabella_prices(prices: List[Dict[str, Any]]) -> tuple:
    """Clasifica los precios de Falabella: (actual, normal, cmr)."""
    current = normal = 0.0
    card: Optional[float] = None
    for p in prices or []:
        ptype = str(p.get("type", "")).lower()
        val = _clean_price(p.get("price"))
        if val <= 0:
            continue
        if "normal" in ptype:
            normal = normal or val
        elif "cmr" in ptype:
            card = val
        elif current == 0.0 or val < current:
            current = val
    if current <= 0 and normal > 0:
        current = normal
    if normal <= 0:
        normal = current
    if card is not None and card >= current:
        card = None
    return current, normal, card


def _infer_category(title: str, default: str) -> str:
    t = title.lower()
    rules = (
        (("notebook", "laptop", "macbook"), "notebooks"),
        (("celular", "smartphone", "iphone", "galaxy"), "smartphones"),
        (("smart tv", "televisor", "qled", "oled"), "televisores"),
        (("consola", "playstation", "xbox", "nintendo switch"), "consolas"),
        (("tarjeta de video", "rtx", "radeon"), "componentes"),
    )
    for keys, cat in rules:
        if any(k in t for k in keys):
            return cat
    if default == OTHER_CATEGORY:
        return classify_category(title)
    return default


def parse_falabella_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """Productos de una página de categoría o búsqueda de Falabella."""
    data = _extract_next_data(html)
    results = data.get("props", {}).get("pageProps", {}).get("results") or []
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in results:
        title = str(raw.get("displayName") or "").strip()
        url = raw.get("url") or ""
        pid = canonical_product_id("falabella", url)
        current, normal, card = _falabella_prices(raw.get("prices"))
        if not title or not pid or current <= 0:
            continue
        media = raw.get("mediaUrls") or []
        items.append(ProductItem(
            id=pid, store_id="falabella", store_name=STORE_NAMES["falabella"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=card, url=url, image_url=media[0] if media else None, in_stock=True,
            timestamp=now, source="direct",
        ))
    return items


_NEXT_F_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def _paris_products(html: str) -> List[Dict[str, Any]]:
    """
    Productos del flujo RSC de Next.js App Router de Paris (self.__next_f): objetos con formato
    commercetools ('masterVariant.prices' con regular / offer / paymentMethod).
    """
    flight = _next_flight(html)
    decoder = json.JSONDecoder()
    found: Dict[str, Dict[str, Any]] = {}
    for m in re.finditer(r'"masterVariant"', flight):
        j = m.start()
        for _ in range(400):   # retroceder hasta el inicio del objeto producto
            j = flight.rfind("{", 0, j)
            if j < 0:
                break
            try:
                obj, _ = decoder.raw_decode(flight, j)
            except ValueError:
                continue
            if isinstance(obj, dict) and "masterVariant" in obj:
                key = str(obj.get("key") or obj.get("id") or "")
                if key:
                    found.setdefault(key, obj)
                break
    return list(found.values())


def _cent_amount(price: Any) -> float:
    value = (price or {}).get("value") or {}
    amount = value.get("centAmount")
    digits = int(value.get("fractionDigits") or 0)
    return float(amount) / (10 ** digits) if amount else 0.0


def parse_paris_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """Productos de Paris: precio normal (regular), oferta (offer) y tarjeta Cencosud (paymentMethod)."""
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in _paris_products(html):
        if raw.get("publish") is False:
            continue
        title = " ".join(str(raw.get("name") or "").split())   # Paris usa espacios duros (\xa0)
        slug = str(raw.get("slug") or "").strip()
        mv = raw.get("masterVariant") or {}
        prices = mv.get("prices") or {}
        normal = _cent_amount(prices.get("regular"))
        offer = _cent_amount(prices.get("offer"))
        card = _cent_amount(prices.get("paymentMethod")) or None
        current = offer or normal
        if not title or not slug or current <= 0:
            continue
        url = f"{STORE_BASE['paris']}/{slug}.html"
        pid = canonical_product_id("paris", url)
        if not pid:
            continue
        if card is not None and card >= current:
            card = None
        images = mv.get("images") or []
        image = images[0].get("url") if images and isinstance(images[0], dict) else None
        items.append(ProductItem(
            id=pid, store_id="paris", store_name=STORE_NAMES["paris"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal or current,
            card_price=card, url=url, image_url=image, in_stock=True, timestamp=now, source="direct",
        ))
    return items


def parse_paris_product(html: str, url: str) -> Optional[ProductItem]:
    """Página de producto de Paris: el producto de la URL entre los objetos del flujo RSC."""
    target = canonical_product_id("paris", url)
    for item in parse_paris_listing(html):
        if item.id == target:
            return item
    return None


def _next_flight(html: str) -> str:
    """Texto del flujo RSC de Next.js App Router (self.__next_f) de la página."""
    chunks = _NEXT_F_RE.findall(html or "")
    if not chunks:
        raise BlockedError("página sin self.__next_f")
    return "".join(json.loads('"' + c + '"') for c in chunks)


def parse_jumbo_listing(html: str, category_hint: str = "tecnologia", store_id: str = "jumbo") -> List[ProductItem]:
    """
    Productos de Jumbo: objetos "product" del flujo RSC con items[0].price (precio actual),
    listPrice (normal) y stock. La ficha usa los mismos objetos (el producto y sus relacionados).
    Santa Isabel (también Cencosud) usa el mismo sitio, con store_id="santaisabel".
    """
    flight = _next_flight(html)
    decoder = json.JSONDecoder()
    items: List[ProductItem] = []
    seen: set = set()
    now = datetime.now(timezone.utc)
    for m in re.finditer(r'"product":\{"productId"', flight):
        try:
            raw, _ = decoder.raw_decode(flight, m.start() + len('"product":'))
        except ValueError:
            continue
        slug = str(raw.get("slug") or "").strip()
        sku = (raw.get("items") or [{}])[0] or {}
        url = f"{STORE_BASE[store_id]}/{slug}/p"
        pid = canonical_product_id(store_id, url)
        if not pid or pid in seen:
            continue
        title = " ".join(str(sku.get("name") or "").split())
        try:
            current = float(sku.get("price") or 0)
            normal = float(sku.get("listPrice") or 0)
        except (TypeError, ValueError):
            continue
        if not title or current <= 0:
            continue
        seen.add(pid)
        images = sku.get("images") or []
        items.append(ProductItem(
            id=pid, store_id=store_id, store_name=STORE_NAMES[store_id], title=title,
            category=_infer_category(title, category_hint), current_price=current,
            normal_price=max(normal, current), card_price=None, url=url,
            image_url=images[0] if images and str(images[0]).startswith("http") else None,
            in_stock=bool(sku.get("stock", True)), timestamp=now, source="direct",
        ))
    return items


def parse_jumbo_product(html: str, url: str, store_id: str = "jumbo") -> Optional[ProductItem]:
    """Ficha de Jumbo (o Santa Isabel): el producto de la URL entre los objetos del flujo RSC."""
    target = canonical_product_id(store_id, url)
    for item in parse_jumbo_listing(html, store_id=store_id):
        if item.id == target:
            return item
    return None


def parse_santaisabel_listing(html: str, category_hint: str = "otros") -> List[ProductItem]:
    return parse_jumbo_listing(html, category_hint, store_id="santaisabel")


def parse_santaisabel_product(html: str, url: str) -> Optional[ProductItem]:
    return parse_jumbo_product(html, url, store_id="santaisabel")


def parse_vtex_products(store_id: str, products: Any) -> List[ProductItem]:
    """
    Productos de la API de catálogo VTEX: el primer SKU con su primer vendedor
    (commertialOffer.Price = actual, ListPrice = normal, AvailableQuantity = stock).
    """
    if not isinstance(products, list):
        raise BlockedError("respuesta VTEX inesperada")
    name, _, hint = VTEX_STORES[store_id]
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in products:
        try:
            sku = (raw.get("items") or [])[0]
            offer = (sku.get("sellers") or [])[0].get("commertialOffer") or {}
            current = float(offer.get("Price") or 0)
            normal = current if store_id in VTEX_INFLATED_LIST_PRICE else float(offer.get("ListPrice") or 0)
            # Productos vendidos por unidad de medida (pisos por m², telas por metro): Price viene por
            # unidad y ListPrice no, así que compararlos inventa descuentos del 70% (MK: porcelanato a
            # $13.990,27 el m² con "normal" $59.790). PriceWithoutDiscount sí está en la misma unidad.
            multiplier = float(sku.get("unitMultiplier") or 1)
            if abs(multiplier - 1.0) > 1e-6:
                normal = float(offer.get("PriceWithoutDiscount") or current)
        except (AttributeError, IndexError, TypeError, ValueError):
            continue
        title = " ".join(str(raw.get("productName") or "").split())
        url = str(raw.get("link") or "")
        pid = canonical_product_id(store_id, url)
        if not pid or not title or current <= 0:
            continue
        images = sku.get("images") or []
        items.append(ProductItem(
            id=pid, store_id=store_id, store_name=name, title=title,
            category=_infer_category(title, hint), current_price=current, normal_price=max(normal, current),
            card_price=None, url=url, image_url=images[0].get("imageUrl") if images else None,
            in_stock=int(offer.get("AvailableQuantity") or 0) > 0, timestamp=now, source="direct",
        ))
    return items


def parse_shopify_products(store_id: str, products: Any) -> List[ProductItem]:
    """
    Productos de /products.json de Shopify: la variante disponible más barata
    (price = actual, compare_at_price = normal) y el enlace /products/{handle}.
    """
    if not isinstance(products, list):
        raise BlockedError("respuesta Shopify inesperada")
    name, base, hint = SHOPIFY_STORES[store_id]
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in products:
        variants = [v for v in (raw.get("variants") or []) if isinstance(v, dict)]
        # /products/{handle}.json no trae "available": sin el campo, el stock no se da por agotado
        available = [v for v in variants if v.get("available", True)]
        try:
            variant = min(available or variants, key=lambda v: float(v.get("price") or 0))
            current = float(variant.get("price") or 0)
            normal = 0.0 if store_id in SHOPIFY_INFLATED_COMPARE_PRICE else float(variant.get("compare_at_price") or 0)
        except (TypeError, ValueError):
            continue
        title = " ".join(str(raw.get("title") or "").split())
        handle = str(raw.get("handle") or "").strip()
        url = f"{base}/products/{handle}"
        pid = canonical_product_id(store_id, url)
        if not pid or not title or current <= 0:
            continue
        images = raw.get("images") or []
        items.append(ProductItem(
            id=pid, store_id=store_id, store_name=name, title=title,
            category=_infer_category(title, hint), current_price=current, normal_price=max(normal, current),
            card_price=None, url=url, image_url=images[0].get("src") if images and isinstance(images[0], dict) else None,
            in_stock=bool(available), timestamp=now, source="direct",
        ))
    return items


def _hp_prices(node: Any) -> tuple:
    """(actual, normal) de HP: precio final y, si hay rebaja, el precio anterior (oldPrice) de Magento."""
    def amount(selector: str) -> float:
        el = node.select_one(selector)
        try:
            return float(el["data-price-amount"]) if el else 0.0
        except (TypeError, ValueError, KeyError):
            return 0.0

    current = amount("[data-price-type=finalPrice][data-price-amount]")
    old = amount("[data-price-type=oldPrice][data-price-amount]")
    return current, max(old, current)


def parse_hp_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """Productos de la HP Store Chile (Magento): li.product-item con a.product-item-link y precios data-price-amount."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    tiles = soup.select("li.product-item")
    if not tiles and "data-price-amount" not in (html or ""):
        raise BlockedError("página sin productos")
    items: List[ProductItem] = []
    seen: set = set()
    now = datetime.now(timezone.utc)
    for tile in tiles:
        link = tile.select_one("a.product-item-link[href]")
        if link is None:
            continue
        url = link["href"].split("#")[0]
        pid = canonical_product_id("hp", url)
        title = " ".join(link.get_text(" ").split())
        current, normal = _hp_prices(tile)
        if not pid or pid in seen or not title or current <= 0:
            continue
        seen.add(pid)
        img = tile.select_one("img[src^='http']")
        items.append(ProductItem(
            id=pid, store_id="hp", store_name=STORE_NAMES["hp"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=None, url=url, image_url=img["src"] if img else None,
            in_stock="agotado" not in tile.get_text(" ").lower(), timestamp=now, source="direct",
        ))
    return items


def parse_magento_listing(store_id: str, html: str, category_hint: Optional[str] = None) -> tuple:
    """
    (productos, total) de un listado Magento 2: li.product-item con su enlace (a.product-item-link,
    o el de .product-item-name) y precios data-price-amount (finalPrice / oldPrice).
    """
    from bs4 import BeautifulSoup

    name, base, hint, _ = MAGENTO_STORES[store_id]
    soup = BeautifulSoup(html or "", "html.parser")
    tiles = soup.select("li.product-item")
    if not tiles and "data-price-amount" not in (html or ""):
        raise BlockedError("página sin productos")
    items: List[ProductItem] = []
    seen: set = set()
    now = datetime.now(timezone.utc)
    for tile in tiles:
        link = tile.select_one("a.product-item-link[href]") or tile.select_one(".product-item-name a[href]")
        if link is None:
            continue
        url = urllib.parse.urljoin(base, link["href"].split("?")[0].split("#")[0])
        pid = canonical_product_id(store_id, url)
        title = " ".join((link.get("title") or link.get_text(" ")).split())
        current, normal = _hp_prices(tile)
        if not pid or pid in seen or not title or current <= 0:
            continue
        seen.add(pid)
        img = tile.select_one("img[data-src^='http']") or tile.select_one("img[src^='http']")
        items.append(ProductItem(
            id=pid, store_id=store_id, store_name=name, title=title,
            category=_infer_category(title, category_hint or hint), current_price=current, normal_price=normal,
            card_price=None, url=url, image_url=(img.get("data-src") or img.get("src")) if img else None,
            in_stock="agotado" not in tile.get_text(" ").lower(), timestamp=now, source="direct",
        ))
    toolbar = soup.select_one(".toolbar-amount") or soup.select_one(".toolbar-number")
    numbers = re.findall(r"\d+", (toolbar.get_text(" ") if toolbar else "").replace(".", "").replace(",", ""))   # "de 2.435"
    total = int(numbers[-1]) if numbers else None
    return items, total


def parse_magento_product(store_id: str, html: str, url: str) -> Optional[ProductItem]:
    """Ficha Magento 2: nombre en el h1 y precios en .product-info-main (igual que HP)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    pid = canonical_product_id(store_id, url)
    main = soup.select_one(".product-info-main") or soup
    h1 = soup.select_one("h1")
    title = " ".join(h1.get_text(" ").split()) if h1 else ""
    current, normal = _hp_prices(main)
    if not pid or not title or current <= 0:
        return None
    stock = main.select_one(".stock")
    name, _, hint, _ = MAGENTO_STORES[store_id]
    return ProductItem(
        id=pid, store_id=store_id, store_name=name, title=title, category=_infer_category(title, hint),
        current_price=current, normal_price=normal, card_price=None, url=url,
        in_stock=not (stock and "agotado" in stock.get_text(" ").lower()),
        timestamp=datetime.now(timezone.utc), source="direct",
    )


def parse_hp_product(html: str, url: str) -> Optional[ProductItem]:
    """Ficha de HP: nombre en el h1 y precios en el bloque .product-info-main."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    pid = canonical_product_id("hp", url)
    main = soup.select_one(".product-info-main") or soup
    h1 = soup.select_one("h1")
    title = " ".join(h1.get_text(" ").split()) if h1 else ""
    current, normal = _hp_prices(main)
    if not pid or not title or current <= 0:
        return None
    stock = main.select_one(".stock")
    in_stock = not (stock and "agotado" in stock.get_text(" ").lower())
    return ProductItem(
        id=pid, store_id="hp", store_name=STORE_NAMES["hp"], title=title,
        category=_infer_category(title, "tecnologia"), current_price=current, normal_price=normal,
        card_price=None, url=url, in_stock=in_stock, timestamp=datetime.now(timezone.utc), source="direct",
    )


def _clp(text: str) -> float:
    digits = re.sub(r"[^\d]", "", text or "")
    return float(digits) if digits else 0.0


def parse_winpy_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """
    Productos de Winpy: <article> con p.model (nombre y enlace /venta/…/) y p.valor (precio).
    El listado muestra el precio por transferencia (el más bajo); la ficha agrega un "antes" más alto.
    Para que listado y ficha coincidan (la confirmación los compara), el precio de referencia es el
    mismo precio mostrado y las bajas reales se detectan contra el historial propio.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    tiles = [a for a in soup.select("article") if a.select_one("p.valor")]
    if not tiles and 'class="valor"' not in (html or ""):
        raise BlockedError("página sin productos")
    items: List[ProductItem] = []
    seen: set = set()
    now = datetime.now(timezone.utc)
    for tile in tiles:
        link = tile.select_one("p.model a[href]")
        if link is None:
            continue
        url = urllib.parse.urljoin(STORE_BASE["winpy"], link["href"].split("?")[0])
        pid = canonical_product_id("winpy", url)
        title = " ".join((link.get("title") or link.get_text(" ")).split())
        current = _clp(tile.select_one("p.valor").get_text())
        if not pid or pid in seen or not title or current <= 0:
            continue
        seen.add(pid)
        img = tile.select_one("img[src]")
        image = urllib.parse.urljoin(STORE_BASE["winpy"], img["src"]) if img else None
        items.append(ProductItem(
            id=pid, store_id="winpy", store_name=STORE_NAMES["winpy"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=current,
            card_price=None, url=url, image_url=image,
            in_stock="agotado" not in tile.get_text(" ").lower(), timestamp=now, source="direct",
        ))
    return items


def parse_winpy_product(html: str, url: str) -> Optional[ProductItem]:
    """Ficha de Winpy: h1, precio por transferencia en .price-oferta h2 y disponibilidad en #stock-product."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    pid = canonical_product_id("winpy", url)
    h1 = soup.select_one("h1")
    price = soup.select_one(".price-oferta h2")
    title = " ".join(h1.get_text(" ").split()) if h1 else ""
    current = _clp(price.get_text()) if price else 0.0
    if not pid or not title or current <= 0:
        return None
    stock = soup.select_one("#stock-product")
    in_stock = not (stock and "agotado" in stock.get_text(" ").lower())
    return ProductItem(
        id=pid, store_id="winpy", store_name=STORE_NAMES["winpy"], title=title,
        category=_infer_category(title, "tecnologia"), current_price=current, normal_price=current,
        card_price=None, url=url, in_stock=in_stock, timestamp=datetime.now(timezone.utc), source="direct",
    )


_CLP_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{3})+")


def _spdigital_prices(texts: List[str]) -> tuple:
    """(actual, normal) de SP Digital a partir de sus precios en orden: normal, transferencia, otros medios."""
    prices = [_clp(t) for t in texts if _CLP_RE.fullmatch(t.strip())]
    if len(prices) >= 3:
        return prices[1], max(prices[0], prices[1])   # transferencia (el más bajo) y precio normal
    if prices:
        return prices[0], prices[0]
    return 0.0, 0.0


def parse_spdigital_listing(html: str, category_hint: str = "tecnologia") -> tuple:
    """(productos, última página) de un listado de SP Digital: tarjetas Fractal-ProductCard."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    cards = soup.select('div[data-fractalds="t:m|n:productcard|v:default"]')
    if not cards:
        raise BlockedError("página sin productos")
    items: List[ProductItem] = []
    seen: set = set()
    now = datetime.now(timezone.utc)
    for card in cards:
        links = [a for a in card.select("a[href]") if a.get_text(strip=True)]
        if not links:
            continue
        link = max(links, key=lambda a: len(a.get_text(strip=True)))   # el nombre, no la marca
        url = urllib.parse.urljoin(STORE_BASE["spdigital"], link["href"].split("?")[0])
        pid = canonical_product_id("spdigital", url)
        title = " ".join(link.get_text(" ").split())
        box = card.select_one(".Fractal-ProductCard__price--container")
        current, normal = _spdigital_prices(list(box.stripped_strings) if box else [])
        if not pid or pid in seen or not title or current <= 0:
            continue
        seen.add(pid)
        img = card.select_one("img[src]")
        image = urllib.parse.urljoin("https:", img["src"]) if img else None
        items.append(ProductItem(
            id=pid, store_id="spdigital", store_name=STORE_NAMES["spdigital"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=None, url=url, image_url=image,
            in_stock="agotado" not in card.get_text(" ").lower(), timestamp=now, source="direct",
        ))
    pages = [int(n) for n in re.findall(r'href="/categories/[a-z0-9-]+/(\d+)/', html or "")]
    return items, max(pages + [1])


def parse_spdigital_product(html: str, url: str) -> Optional[ProductItem]:
    """Ficha de SP Digital: h1 y, a continuación, precio normal, transferencia y otros medios de pago."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    pid = canonical_product_id("spdigital", url)
    h1 = soup.select_one("h1")
    if not pid or h1 is None:
        return None
    texts = [t for t in h1.find_all_next(string=_CLP_RE)][:3]
    current, normal = _spdigital_prices([str(t) for t in texts])
    title = " ".join(h1.get_text(" ").split())
    if not title or current <= 0:
        return None
    page_text = soup.get_text(" ").lower()
    return ProductItem(
        id=pid, store_id="spdigital", store_name=STORE_NAMES["spdigital"], title=title,
        category=_infer_category(title, "tecnologia"), current_price=current, normal_price=normal,
        card_price=None, url=url, in_stock="agotado" not in page_text,
        timestamp=datetime.now(timezone.utc), source="direct",
    )


def _abc_price(node: Any, selector: str) -> float:
    span = node.select_one(f"{selector} .price-value[data-value]")
    try:
        return float(span["data-value"]) if span else 0.0
    except (TypeError, ValueError):
        return 0.0


def _abc_prices(node: Any) -> tuple:
    """(tarjeta, actual, referencia) de ABC: la referencia es el precio internet (el "normal" está inflado)."""
    card = _abc_price(node, ".js-tlp-price")
    internet = _abc_price(node, ".js-internet-price")
    listed_normal = _abc_price(node, ".js-normal-price")
    current = internet or card or listed_normal
    if not card or card >= current:
        card = None
    return card, current, current


def parse_abc_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """
    Productos de ABC (ex La Polar; Salesforce Commerce Cloud). Cada tarjeta
    div.product-tile__wrapper[data-pid] trae sus precios en spans .price-value[data-value]:
    .js-tlp-price (tarjeta ABC/La Polar), .js-internet-price (internet) y .js-normal-price ("normal").

    El "normal" de ABC está inflado en todo el catálogo (un LED 55" LG: "normal" $1.799.990,
    se vende a $583.992; los 48 TVs de la categoría aparecían con más de 30% de descuento).
    Como con PC Factory, la referencia es el precio internet: las bajas reales se detectan contra
    el historial propio y la referencia de mercado, no contra el precio tachado de la tienda.
    """
    from bs4 import BeautifulSoup
    import html as html_lib

    soup = BeautifulSoup(html or "", "html.parser")
    tiles = soup.select("div.product-tile__wrapper[data-pid]")
    if not tiles and "data-pid" not in (html or ""):
        raise BlockedError("página sin tarjetas de producto")

    items: List[ProductItem] = []
    seen: set = set()
    now = datetime.now(timezone.utc)
    for tile in tiles:
        link = tile.select_one("a[href$='.html']")
        href = link.get("href") if link else ""
        url = href if href.startswith("http") else f"{STORE_BASE['abc']}{href}"
        pid = canonical_product_id("abc", url)
        if not pid or pid in seen:
            continue
        title = ""
        for el in tile.select("[data-gtm]"):
            try:
                data = json.loads(html_lib.unescape(el.get("data-gtm") or ""))
            except ValueError:
                continue
            name = ((data.get("ecommerce") or {}).get("impressions") or {}).get("name") or (
                data.get("action") if data.get("event") == "ProductName" else None
            )
            if name:
                title = " ".join(str(name).split())
                break
        card, current, normal = _abc_prices(tile)
        if not title or current <= 0:
            continue
        img = tile.select_one("img")
        image = (img.get("data-src") or img.get("src")) if img else None
        seen.add(pid)
        items.append(ProductItem(
            id=pid, store_id="abc", store_name=STORE_NAMES["abc"], title=title,
            category=_infer_category(title, category_hint), current_price=current,
            normal_price=normal, card_price=card, url=url,
            image_url=image if image and image.startswith("http") else None,
            in_stock="agotado" not in tile.get_text(" ").lower(), timestamp=now, source="direct",
        ))
    return items


def parse_abc_product(html: str, url: str) -> Optional[ProductItem]:
    """Ficha de ABC: div.pdp-wrapper[data-pid] con las mismas clases de precio y el nombre en el h1."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    pid = canonical_product_id("abc", url)
    wrapper = soup.select_one("div.pdp-wrapper[data-pid]")
    if not pid or wrapper is None:
        return None
    card, current, normal = _abc_prices(wrapper)
    h1 = soup.select_one("h1")
    title = " ".join(h1.get_text(" ").split()) if h1 else ""
    if not title or current <= 0:
        return None
    return ProductItem(
        id=pid, store_id="abc", store_name=STORE_NAMES["abc"], title=title,
        category=_infer_category(title, "tecnologia"), current_price=current, normal_price=normal,
        card_price=card, url=url, in_stock="agotado" not in soup.get_text(" ").lower(),
        timestamp=datetime.now(timezone.utc), source="direct",
    )


def parse_sodimac_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """
    Productos de Sodimac en sus dos formatos:
    - páginas /lista/ (catálogo del grupo Falabella): pageProps.results, con URL y precios eventPrice/normalPrice;
    - búsqueda: pageProps.searchProps.searchData.results, precios INTERNET/NORMAL y sin URL. Su ficha
      /sodimac-cl/product/{id} redirige a la portada, así que se enlaza como búsqueda por SKU
      (/sodimac-cl/search?Ntt={sku}), que devuelve exactamente ese producto.
    """
    data = _extract_next_data(html)
    pp = data.get("props", {}).get("pageProps", {}) or {}
    results = pp.get("results") or ((pp.get("searchProps") or {}).get("searchData") or {}).get("results") or []
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in results:
        title = str(raw.get("displayName") or "").strip()
        pid_raw = str(raw.get("productId") or "").strip()
        sku = str(raw.get("skuId") or pid_raw)
        url = raw.get("url") or (f"{STORE_BASE['sodimac']}/sodimac-cl/search?Ntt={urllib.parse.quote(sku)}" if sku else "")
        pid = canonical_product_id("sodimac", url)
        current, normal, card = _falabella_prices(raw.get("prices"))
        if not title or not pid or current <= 0:
            continue
        media = raw.get("mediaUrls") or []
        items.append(ProductItem(
            id=pid, store_id="sodimac", store_name=STORE_NAMES["sodimac"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=card, url=url, image_url=media[0] if media else None, in_stock=True,
            timestamp=now, source="direct",
        ))
    return items


def parse_sodimac_product(html: str, url: str) -> Optional[ProductItem]:
    """Página de producto /articulo/ de Sodimac (mismo formato productData que Falabella)."""
    item = parse_falabella_product(html, url)
    if item is None:
        return None
    return item.model_copy(update={
        "id": canonical_product_id("sodimac", url) or item.id.replace("falabella-", "sodimac-", 1),
        "store_id": "sodimac",
        "store_name": STORE_NAMES["sodimac"],
    })


def parse_falabella_product(html: str, url: str) -> Optional[ProductItem]:
    """Producto individual (página de detalle) de Falabella."""
    data = _extract_next_data(html)
    pd = data.get("props", {}).get("pageProps", {}).get("productData") or {}
    variants = pd.get("variants") or []
    if not pd or not variants:
        return None
    v = variants[0]
    current, normal, card = _falabella_prices(v.get("prices"))
    pid = canonical_product_id("falabella", url) or f"falabella-{str(pd.get('id', '')).lower()}"
    if current <= 0:
        return None
    return ProductItem(
        id=pid, store_id="falabella", store_name=STORE_NAMES["falabella"], title=str(pd.get("name", "")).strip(),
        category=_infer_category(str(pd.get("name", "")), "tecnologia"), current_price=current,
        normal_price=normal, card_price=card, url=url, in_stock=bool(v.get("isPurchaseable", True)),
        timestamp=datetime.now(timezone.utc), source="direct",
    )


def _ripley_product_urls(html: str) -> Dict[str, str]:
    """Mapa id canónico -> URL absoluta, a partir de los enlaces de la página."""
    urls: Dict[str, str] = {}
    for href in re.findall(r'href="(/[^"?#]+)', html):
        pid = canonical_product_id("ripley", href)
        if pid and pid not in urls:
            urls[pid] = STORE_BASE["ripley"] + href
    return urls


def parse_ripley_listing(html: str, category_hint: str = "tecnologia") -> List[ProductItem]:
    """Productos de una página de categoría o búsqueda de Ripley."""
    data = _extract_next_data(html)
    fp = data.get("props", {}).get("pageProps", {}).get("findabilityProps") or {}
    products = (fp.get("data") or {}).get("products") or []
    links = _ripley_product_urls(html)
    items: List[ProductItem] = []
    now = datetime.now(timezone.utc)
    for raw in products:
        title = str(raw.get("name") or "").strip()
        pid = ripley_id_from_parent(raw.get("parentProductID"))
        current = _clean_price(raw.get("priceNumber"))
        if not title or not pid or current <= 0:
            continue
        normal = _clean_price(raw.get("masterPriceNumber")) or current
        card = _clean_price(raw.get("ripleyPriceNumber")) or None
        if card is not None and card >= current:
            card = None
        token = pid.split("-", 1)[1]
        url = links.get(pid) or f"{STORE_BASE['ripley']}/{token}{'p' if token.isdigit() else ''}"
        image = raw.get("primaryImage")
        items.append(ProductItem(
            id=pid, store_id="ripley", store_name=STORE_NAMES["ripley"], title=title,
            category=_infer_category(title, category_hint), current_price=current, normal_price=normal,
            card_price=card, url=url, image_url=("https:" + image) if isinstance(image, str) and image.startswith("//") else image,
            in_stock=bool(raw.get("inStock", True)), timestamp=now, source="direct",
        ))
    return items


def parse_ripley_product(html: str, url: str) -> Optional[ProductItem]:
    """Producto individual (página de detalle) de Ripley."""
    data = _extract_next_data(html)
    product = (((data.get("props", {}).get("pageProps", {}).get("detailProps") or {}).get("data") or {}).get("product")) or {}
    pps = product.get("parentpricestock") or {}
    price = pps.get("price") or {}
    current = _clean_price((price.get("sale") or {}).get("valueNumber"))
    normal = _clean_price((price.get("master") or {}).get("valueNumber")) or current
    card = _clean_price((price.get("ripley") or {}).get("valueNumber")) or None
    if not product or current <= 0:
        return None
    if card is not None and card >= current:
        card = None
    pid = canonical_product_id("ripley", url) or ripley_id_from_parent(product.get("parentProductID"))
    title = str(product.get("name", "")).strip()
    return ProductItem(
        id=pid, store_id="ripley", store_name=STORE_NAMES["ripley"], title=title,
        category=_infer_category(title, "tecnologia"), current_price=current, normal_price=normal,
        card_price=card, url=url, in_stock=bool(pps.get("stock", True)),
        timestamp=datetime.now(timezone.utc), source="direct",
    )


LISTING_PARSERS = {
    "falabella": parse_falabella_listing,
    "ripley": parse_ripley_listing,
    "paris": parse_paris_listing,
    "abc": parse_abc_listing,
    "jumbo": parse_jumbo_listing,
    "hp": parse_hp_listing,
    "winpy": parse_winpy_listing,
    "sodimac": parse_sodimac_listing,
}
PRODUCT_PARSERS = {
    "falabella": parse_falabella_product,
    "ripley": parse_ripley_product,
    "paris": parse_paris_product,
    "abc": parse_abc_product,
    "jumbo": parse_jumbo_product,
    "hp": parse_hp_product,
    "winpy": parse_winpy_product,
    "sodimac": parse_sodimac_product,
    "spdigital": parse_spdigital_product,
    "santaisabel": parse_santaisabel_product,
}


class DirectStoreReader:
    """Consulta directa de Falabella y Ripley con freno automático ante bloqueos."""

    def __init__(self, client: Any = None, backoff: Optional[StoreBackoff] = None, timeout: float = 12.0):
        self.client = client or default_stealth_client
        self.backoff = backoff or StoreBackoff()
        self.timeout = timeout
        self._catalog_cursor: Dict[str, int] = {}
        self._catalog_pages: Dict[str, int] = {}
        self._magento_cursor: Dict[str, tuple] = {}
        self._magento_page_size: Dict[str, int] = {}   # productos por página que entrega cada tienda
        self._santaisabel_seen: set = set()       # IDs de la sección en curso (detecta la última página)
        self._shopify_lock = threading.Lock()
        self.shopify_stores: Optional[List[str]] = None   # None = todas; lo fija el lector continuo
        self._shopify_turn = 0                 # índice de la tienda Shopify a la que le toca
        self._shopify_next_at = 0.0            # instante (monotónico) de la próxima petición Shopify
        self._shopify_hot_turn = 0             # siguiente tienda del carril rápido Shopify
        self._shopify_hot_next = False         # alterna turnos de carril rápido y de recorrido
        self._shopify_sale: Dict[str, Optional[str]] = {}   # colección de ofertas (None = no tiene)
        self.clock: Callable[[], float] = time.monotonic

    # --- PC Factory (API JSON) -------------------------------------------------
    def _pcfactory_query(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Consulta la API de PC Factory con el mismo freno automático que las páginas HTML."""
        if self.backoff.is_paused("pcfactory"):
            return None
        try:
            resp = self.client.get(PCFACTORY_API, params=params, headers=PCFACTORY_HEADERS, timeout=self.timeout)
        except Exception as exc:
            logger.debug("Error de red en la API de PC Factory: %s", exc)
            return None
        status = getattr(resp, "status_code", 0)
        if status in (403, 429) or status >= 500:
            self.backoff.record_block("pcfactory", f"HTTP {status}")
            return None
        if status != 200:
            return None
        try:
            content = resp.json().get("content") or {}
        except Exception:
            self.backoff.record_block("pcfactory", "respuesta no es JSON")
            return None
        self.backoff.record_success("pcfactory")
        return content

    def _pcfactory_items(self, content: Optional[Dict[str, Any]]) -> List[ProductItem]:
        from glitchhunter_ai.scrapers.pcfactory import PCFactoryScraper

        if not content:
            return []
        return PCFactoryScraper().parse_pcfactory_json(content.get("items") or [])

    # --- SP Digital (listados HTML por sección) ---------------------------------
    def _spdigital_catalog_slice(self) -> List[ProductItem]:
        """Las siguientes páginas de las secciones de SP Digital (cursor: sección y página)."""
        items: List[ProductItem] = []
        section_idx, page = self._magento_cursor.get("spdigital", (0, 1))
        for _ in range(SPDIGITAL_PAGES_PER_CYCLE):
            path = SPDIGITAL_SECTIONS[section_idx % len(SPDIGITAL_SECTIONS)]
            html = self._get_html("spdigital", f"{STORE_BASE['spdigital']}{path}{f'{page}/' if page > 1 else ''}")
            if html is None:
                break
            try:
                page_items, last_page = parse_spdigital_listing(html)
            except (BlockedError, ValueError, KeyError) as exc:
                self.backoff.record_block("spdigital", f"formato inesperado: {exc}")
                break
            self.backoff.record_success("spdigital")
            items.extend(page_items)
            if not page_items or page >= last_page:
                section_idx, page = (section_idx + 1) % len(SPDIGITAL_SECTIONS), 1   # siguiente sección
            else:
                page += 1
        self._magento_cursor["spdigital"] = (section_idx, page)
        return items

    # --- Santa Isabel (flujo RSC como Jumbo, por secciones) ----------------------
    def _santaisabel_catalog_slice(self, lane: str = "both") -> List[ProductItem]:
        """Las siguientes páginas de las secciones de Santa Isabel (cursor: sección, página e IDs vistos)."""
        items: List[ProductItem] = []
        section_idx, page = self._magento_cursor.get("santaisabel", (0, 1))
        if lane == "hot" or (lane == "both" and (section_idx % len(SANTAISABEL_SECTIONS), page) != (0, 1)):
            # Carril rápido: /ofertas se relee en cada ciclo
            html = self._get_html("santaisabel", f"{STORE_BASE['santaisabel']}{SANTAISABEL_SECTIONS[0]}")
            if html is None:
                return items
            try:
                items.extend(parse_santaisabel_listing(html))
            except (BlockedError, ValueError, KeyError) as exc:
                self.backoff.record_block("santaisabel", f"formato inesperado: {exc}")
                return items
            if lane == "hot":
                self.backoff.record_success("santaisabel")
                return items
        for _ in range(SANTAISABEL_PAGES_PER_CYCLE):
            path = SANTAISABEL_SECTIONS[section_idx % len(SANTAISABEL_SECTIONS)]
            html = self._get_html("santaisabel", f"{STORE_BASE['santaisabel']}{path}" + (f"?page={page}" if page > 1 else ""))
            if html is None:
                break
            try:
                page_items = parse_santaisabel_listing(html)
            except (BlockedError, ValueError, KeyError) as exc:
                self.backoff.record_block("santaisabel", f"formato inesperado: {exc}")
                break
            self.backoff.record_success("santaisabel")
            seen = self._santaisabel_seen if page > 1 else set()
            new = [i for i in page_items if i.id not in seen]
            items.extend(new)
            self._santaisabel_seen = seen | {i.id for i in page_items}
            if not new or page >= SANTAISABEL_MAX_PAGES:
                section_idx, page = (section_idx + 1) % len(SANTAISABEL_SECTIONS), 1   # siguiente sección
            else:
                page += 1
        self._magento_cursor["santaisabel"] = (section_idx, page)
        return items

    # --- Tiendas Magento 2 (listados HTML) --------------------------------------
    def _magento_catalog_slice(self, store_id: str, lane: str = "both") -> List[ProductItem]:
        """Las siguientes páginas de las secciones de la tienda (cursor: sección y página)."""
        _, base, _, sections = MAGENTO_STORES[store_id]
        items: List[ProductItem] = []
        if lane == "hot" and not magento_has_sale_section(store_id):
            return items
        section_idx, page = self._magento_cursor.get(store_id, (0, 1))
        if lane == "hot" or (lane == "both" and (section_idx % len(sections), page) != (0, 1)):
            # Carril rápido: la primera sección (ofertas/sale en la mayoría) se relee en cada ciclo
            html = self._get_html(store_id, f"{base}{sections[0]}?product_list_limit={MAGENTO_PAGE_SIZE}&p=1")
            if html is None:
                return items
            try:
                items.extend(parse_magento_listing(store_id, html)[0])
            except (BlockedError, ValueError, KeyError) as exc:
                self.backoff.record_block(store_id, f"formato inesperado: {exc}")
                return items
            if lane == "hot":
                self.backoff.record_success(store_id)
                return items
        for _ in range(MAGENTO_PAGES_PER_CYCLE):
            path = sections[section_idx % len(sections)]
            url = f"{base}{path}?product_list_limit={MAGENTO_PAGE_SIZE}&p={page}"
            # Pasado el final de una sección la tienda responde una página sin productos: eso es el fin
            # de la sección, no un bloqueo (solo la primera página vacía cuenta como bloqueo).
            html = self._get_html(store_id, url, allow_empty=page > 1)
            if html is None:
                if page > 1 and not self.backoff.is_paused(store_id):
                    section_idx, page = (section_idx + 1) % len(sections), 1
                    continue
                break
            try:
                page_items, total = parse_magento_listing(store_id, html)
            except (BlockedError, ValueError, KeyError) as exc:
                self.backoff.record_block(store_id, f"formato inesperado: {exc}")
                break
            self.backoff.record_success(store_id)
            items.extend(page_items)
            # Muchas tiendas ignoran product_list_limit y traen 40 o 32 por página: se usa el tamaño real
            per_page = max(len(page_items), self._magento_page_size.get(store_id, 0))
            if page_items:
                self._magento_page_size[store_id] = per_page
            last_page = (not page_items or (total and page * per_page >= total)
                         or (not total and len(page_items) < per_page))
            if last_page:
                section_idx, page = (section_idx + 1) % len(sections), 1   # siguiente sección
            else:
                page += 1
        self._magento_cursor[store_id] = (section_idx, page)
        return items

    # --- Tiendas Shopify (/products.json) ---------------------------------------
    def _shopify_get(self, store_id: str, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        """GET a una tienda Shopify con la pausa compartida de Shopify. Retorna el JSON o None."""
        if self.backoff.is_paused(SHOPIFY_BACKOFF_KEY) or self.backoff.is_paused(store_id):
            return None
        try:
            resp = self.client.get(f"{SHOPIFY_STORES[store_id][1]}{path}", params=params or {}, timeout=self.timeout)
        except Exception as exc:
            logger.debug("Error de red en Shopify (%s): %s", store_id, exc)
            return None
        status = getattr(resp, "status_code", 0)
        if status in (403, 429):
            self.backoff.record_block(SHOPIFY_BACKOFF_KEY, f"HTTP {status} en {store_id}")
            return None
        if status >= 500:
            self.backoff.record_block(store_id, f"HTTP {status}")
            return None
        if status != 200:
            return None
        try:
            data = resp.json()
        except Exception:
            self.backoff.record_block(SHOPIFY_BACKOFF_KEY, f"respuesta no es JSON en {store_id}")
            return None
        self.backoff.record_success(SHOPIFY_BACKOFF_KEY)
        self.backoff.record_success(store_id)
        return data

    def _shopify_catalog_slice(self, store_id: str) -> List[ProductItem]:
        """Una página de 250 productos, solo si es el turno de esta tienda y pasó el espaciado."""
        order = list(SHOPIFY_STORES) if self.shopify_stores is None else list(self.shopify_stores)
        if not order:
            return None
        with self._shopify_lock:
            now = self.clock()
            if order[self._shopify_turn % len(order)] != store_id or now < self._shopify_next_at:
                return []
            self._shopify_turn += 1
            self._shopify_next_at = now + SHOPIFY_SPACING_SECONDS
        page = self._catalog_cursor.get(store_id, 1)
        data = self._shopify_get(store_id, "/products.json", {"limit": SHOPIFY_PAGE_SIZE, "page": page})
        if data is None:
            return []
        try:
            items = parse_shopify_products(store_id, (data or {}).get("products"))
        except BlockedError as exc:
            self.backoff.record_block(store_id, f"formato inesperado: {exc}")
            return []
        full_page = len((data or {}).get("products") or []) >= SHOPIFY_PAGE_SIZE
        self._catalog_cursor[store_id] = page + 1 if full_page else 1
        return items

    def _shopify_discover_sale(self, store_id: str) -> None:
        """Busca (una vez) la colección pública de ofertas de la tienda en /collections.json."""
        data = self._shopify_get(store_id, "/collections.json", {"limit": 250})
        if data is None:
            return   # sin respuesta: se reintenta en otra vuelta
        self._shopify_sale[store_id] = pick_shopify_sale_collection((data or {}).get("collections") or [])
        logger.info("🛍️ [Shopify] %s: colección de ofertas %s.", STORE_NAMES.get(store_id, store_id),
                    self._shopify_sale[store_id] or "no encontrada (solo recorrido)")

    def _shopify_hot(self, store_id: str) -> List[ProductItem]:
        handle = self._shopify_sale.get(store_id)
        if not handle:
            return []
        data = self._shopify_get(store_id, f"/collections/{urllib.parse.quote(handle)}/products.json",
                                 {"limit": SHOPIFY_PAGE_SIZE})
        try:
            return parse_shopify_products(store_id, (data or {}).get("products")) if data else []
        except BlockedError as exc:
            self.backoff.record_block(store_id, f"formato inesperado: {exc}")
            return []

    def fetch_shopify_next(self) -> Optional[tuple]:
        """
        Una petición Shopify si pasó el espaciado global: (tienda, carril, productos), o None si aún no toca.
        Los turnos se alternan entre el carril rápido (la colección de ofertas de la siguiente tienda que
        la tenga) y el recorrido del catálogo completo, así que el ritmo total de peticiones no cambia.
        """
        order = list(SHOPIFY_STORES) if self.shopify_stores is None else list(self.shopify_stores)
        if not order:
            return None
        with self._shopify_lock:
            now = self.clock()
            if now < self._shopify_next_at or self.backoff.is_paused(SHOPIFY_BACKOFF_KEY):
                return None
            self._shopify_next_at = now + SHOPIFY_SPACING_SECONDS
            self._shopify_hot_next = not self._shopify_hot_next
            hot_store = None
            if self._shopify_hot_next:
                for _ in range(len(order)):
                    cand = order[self._shopify_hot_turn % len(order)]
                    self._shopify_hot_turn += 1
                    if self._shopify_sale.get(cand, "?") is not None:   # desconocida o con colección
                        hot_store = cand
                        break
            if hot_store is None:
                deep_store = order[self._shopify_turn % len(order)]
                self._shopify_turn += 1
        if hot_store is not None:
            if hot_store not in self._shopify_sale:
                self._shopify_discover_sale(hot_store)
                return hot_store, "hot", []
            return hot_store, "hot", self._shopify_hot(hot_store)
        page = self._catalog_cursor.get(deep_store, 1)
        data = self._shopify_get(deep_store, "/products.json", {"limit": SHOPIFY_PAGE_SIZE, "page": page})
        if data is None:
            return deep_store, "deep", []
        try:
            items = parse_shopify_products(deep_store, (data or {}).get("products"))
        except BlockedError as exc:
            self.backoff.record_block(deep_store, f"formato inesperado: {exc}")
            return deep_store, "deep", []
        full_page = len((data or {}).get("products") or []) >= SHOPIFY_PAGE_SIZE
        self._catalog_cursor[deep_store] = page + 1 if full_page else 1
        return deep_store, "deep", items

    # --- Tiendas VTEX (API pública de catálogo) --------------------------------
    def _vtex_query(self, store_id: str, path: str = "", params: Optional[Dict[str, Any]] = None) -> Optional[tuple]:
        """(productos, total) de la API de catálogo VTEX, con el mismo freno automático que las páginas."""
        if self.backoff.is_paused(store_id):
            return None
        url = f"{VTEX_STORES[store_id][1]}/api/catalog_system/pub/products/search{path}"
        try:
            resp = self.client.get(url, params=params or {}, timeout=self.timeout)
        except Exception as exc:
            logger.debug("Error de red en la API VTEX de %s: %s", store_id, exc)
            return None
        status = getattr(resp, "status_code", 0)
        if status in (403, 429) or status >= 500:
            self.backoff.record_block(store_id, f"HTTP {status}")
            return None
        if status not in (200, 206):   # 206: respuesta paginada
            return None
        try:
            items = parse_vtex_products(store_id, resp.json())
        except Exception:
            self.backoff.record_block(store_id, "respuesta no es JSON de catálogo")
            return None
        self.backoff.record_success(store_id)
        resources = str((getattr(resp, "headers", None) or {}).get("resources") or "")
        total = int(resources.rsplit("/", 1)[-1]) if resources.rsplit("/", 1)[-1].isdigit() else None
        return items, total

    def _vtex_catalog_slice(self, store_id: str, pages: int, lane: str = "both") -> List[ProductItem]:
        items: List[ProductItem] = []
        if lane in ("both", "hot"):
            hot = self._vtex_query(store_id, params={"_from": 0, "_to": VTEX_PAGE_SIZE - 1, "O": VTEX_HOT_ORDER})
            if hot is None:
                return items   # tienda en pausa o sin respuesta: no se sigue con el recorrido
            items.extend(hot[0])
            if lane == "hot":
                return items
        offset = self._catalog_cursor.get(store_id, 0)
        for _ in range(max(1, pages)):
            result = self._vtex_query(store_id, params={
                "_from": offset, "_to": offset + VTEX_PAGE_SIZE - 1, "O": "OrderByReleaseDateDESC",
            })
            if result is None:
                break
            page_items, total = result
            items.extend(page_items)
            offset += VTEX_PAGE_SIZE
            limit = min(total or 0, VTEX_MAX_OFFSET)
            if not page_items or offset >= limit:
                offset = 0
                break
        self._catalog_cursor[store_id] = offset
        return items

    def fetch_hot(self, store_id: str) -> List[ProductItem]:
        """Carril rápido: una petición a lo que más cambia (mejor descuento u ofertas) de la tienda."""
        if store_id in VTEX_STORES:
            return self._vtex_catalog_slice(store_id, 0, lane="hot")
        if store_id in MAGENTO_STORES:
            return self._magento_catalog_slice(store_id, lane="hot")
        if store_id == "santaisabel":
            return self._santaisabel_catalog_slice(lane="hot")
        return []

    def fetch_deep(self, store_id: str, pages: int = 10) -> List[ProductItem]:
        """Recorrido: las siguientes páginas del catálogo completo, sin el carril rápido."""
        if store_id in VTEX_STORES:
            return self._vtex_catalog_slice(store_id, min(pages, VTEX_PAGES_PER_CYCLE), lane="deep")
        if store_id in MAGENTO_STORES:
            return self._magento_catalog_slice(store_id, lane="deep")
        if store_id == "santaisabel":
            return self._santaisabel_catalog_slice(lane="deep")
        if store_id in SHOPIFY_STORES:
            return []   # Shopify va por fetch_shopify_next (espaciado global)
        return self.fetch_catalog_slice(store_id, pages)

    def fetch_catalog_slice(self, store_id: str, pages: int = 10) -> List[ProductItem]:
        """
        Siguiente tramo del catálogo completo de la tienda (rotación por páginas).
        Lo permiten PC Factory (su API lista todo con search=*) y las tiendas VTEX.
        """
        if store_id in VTEX_STORES:
            return self._vtex_catalog_slice(store_id, min(pages, VTEX_PAGES_PER_CYCLE))
        if store_id in SHOPIFY_STORES:
            return self._shopify_catalog_slice(store_id)
        if store_id in MAGENTO_STORES:
            return self._magento_catalog_slice(store_id)
        if store_id == "spdigital":
            return self._spdigital_catalog_slice()
        if store_id == "santaisabel":
            return self._santaisabel_catalog_slice()
        if store_id != "pcfactory":
            return []
        items: List[ProductItem] = []
        start = self._catalog_cursor.get(store_id, 0)
        total = self._catalog_pages.get(store_id)
        page = start
        for _ in range(max(1, pages)):
            content = self._pcfactory_query({"page": page, "size": PCFACTORY_PAGE_SIZE, "search": "*"})
            if content is None:
                break
            total = (content.get("pageable") or {}).get("totalPages") or total
            items.extend(self._pcfactory_items(content))
            page += 1
            if total and page >= total:
                page = 0
                break
        if total:
            self._catalog_pages[store_id] = total
        self._catalog_cursor[store_id] = page
        return items

    def _get_html(self, store_id: str, url: str, allow_empty: bool = False) -> Optional[str]:
        """
        Descarga la página; ante bloqueo activa la pausa de la tienda y retorna None.
        allow_empty: una página sin productos (búsqueda sin resultados) no cuenta como bloqueo,
        salvo que sea una verificación anti-bot.
        """
        if self.backoff.is_paused(store_id):
            logger.debug("Lectura directa de %s en pausa (%.0f s restantes).", store_id, self.backoff.remaining_seconds(store_id))
            return None
        try:
            resp = self.client.get(url, timeout=self.timeout)
        except Exception as exc:
            logger.debug("Error de red en lectura directa de %s: %s", store_id, exc)
            return None
        status = getattr(resp, "status_code", 0)
        if status in (403, 429) or status >= 500:
            self.backoff.record_block(store_id, f"HTTP {status}")
            return None
        if status != 200:
            return None
        html = resp.text or ""
        data_marker = {
            "paris": "self.__next_f", "jumbo": "self.__next_f", "santaisabel": "self.__next_f", "abc": "data-pid",
            "hp": "data-price-amount", "winpy": "/venta/", "spdigital": "Fractal-",
            **{sid: "data-price-amount" for sid in MAGENTO_STORES},
        }.get(store_id, "__NEXT_DATA__")
        if data_marker not in html:
            lower = html[:20000].lower()
            challenged = any(m in lower for m in _BOT_CHALLENGE_MARKERS)
            if allow_empty and not challenged:
                return None
            self.backoff.record_block(store_id, "verificación anti-bot" if challenged else "página sin datos")
            return None
        return html

    def _listing(self, store_id: str, url: str, category_hint: str, tolerant: bool = False) -> List[ProductItem]:
        """tolerant (búsquedas): sin resultados o con un formato distinto no pausa la tienda."""
        html = self._get_html(store_id, url, allow_empty=tolerant)
        if html is None:
            return []
        try:
            items = LISTING_PARSERS[store_id](html, category_hint)
        except (BlockedError, ValueError, KeyError) as exc:
            if not tolerant:
                self.backoff.record_block(store_id, f"formato inesperado: {exc}")
            return []
        self.backoff.record_success(store_id)
        return items

    def fetch_category(self, store_id: str, category_key: str) -> List[ProductItem]:
        path = CATEGORY_PATHS.get(store_id, {}).get(category_key)
        if not path:
            return []
        return self._listing(store_id, STORE_BASE[store_id] + path, category_key)

    def search(self, store_id: str, keyword: str) -> List[ProductItem]:
        q = urllib.parse.quote(keyword.strip())
        if store_id in VTEX_STORES:
            result = self._vtex_query(store_id, params={"ft": keyword.strip(), "_from": 0, "_to": VTEX_PAGE_SIZE - 1})
            return result[0] if result else []
        if store_id == "falabella":
            url = f"{STORE_BASE['falabella']}/falabella-cl/search?Ntt={q}"
        elif store_id == "ripley":
            url = f"{STORE_BASE['ripley']}/search/{q}"
        elif store_id == "paris":
            url = f"{STORE_BASE['paris']}/search?q={q}"
        elif store_id == "abc":
            url = f"{STORE_BASE['abc']}/on/demandware.store/Sites-Abc-Site/es_CL/Search-Show?q={q}&sz=48"
        elif store_id == "sodimac":
            url = f"{STORE_BASE['sodimac']}/sodimac-cl/search?Ntt={q}"
        elif store_id in ("jumbo", "santaisabel"):
            return []   # la búsqueda de Jumbo responde 404 a peticiones directas
        elif store_id in ("hp", "winpy", "spdigital"):
            return []   # catálogos chicos: basta con las categorías
        elif store_id in SHOPIFY_STORES:
            return []   # cada petición cuenta para el freno de Shopify: basta con el catálogo rotativo
        elif store_id in MAGENTO_STORES:
            url = f"{MAGENTO_STORES[store_id][1]}/catalogsearch/result/?q={q}&product_list_limit={MAGENTO_PAGE_SIZE}"
            html = self._get_html(store_id, url, allow_empty=True)   # sin resultados no es un bloqueo
            try:
                return parse_magento_listing(store_id, html)[0] if html else []
            except (BlockedError, ValueError, KeyError):
                return []
        elif store_id == "pcfactory":
            return self._pcfactory_items(
                self._pcfactory_query({"page": 0, "size": PCFACTORY_PAGE_SIZE, "search": keyword.strip()})
            )
        else:
            return []
        return self._listing(store_id, url, "tecnologia", tolerant=True)

    def fetch_product(self, store_id: str, url: str) -> Optional[ProductItem]:
        """Precio en tiempo real de un producto. None si la tienda no responde o está en pausa."""
        if store_id == "pcfactory":
            pid = (canonical_product_id("pcfactory", url) or "").split("-", 1)[-1]
            if not pid.isdigit():
                return None
            content = self._pcfactory_query({"page": 0, "size": 5, "search": pid})
            matches = [i for i in self._pcfactory_items(content) if i.id == f"pcfactory-{pid}"]
            return matches[0] if matches else None
        if store_id in VTEX_STORES:
            slug = (canonical_product_id(store_id, url) or "").split("-", 1)[-1]
            result = self._vtex_query(store_id, path=f"/{urllib.parse.quote(slug)}/p") if slug else None
            matches = [i for i in (result[0] if result else []) if i.id == f"{store_id}-{slug}"]
            return matches[0] if matches else None
        if store_id in SHOPIFY_STORES:
            handle = (canonical_product_id(store_id, url) or "").split("-", 1)[-1]
            data = self._shopify_get(store_id, f"/products/{urllib.parse.quote(handle)}.json") if handle else None
            product = (data or {}).get("product")
            items = parse_shopify_products(store_id, [product]) if product else []
            return items[0] if items else None
        if store_id in MAGENTO_STORES:
            html = self._get_html(store_id, url)
            return parse_magento_product(store_id, html, url) if html else None
        if store_id not in PRODUCT_PARSERS:
            return None
        html = self._get_html(store_id, url)
        if html is None:
            return None
        try:
            item = PRODUCT_PARSERS[store_id](html, url)
        except (BlockedError, ValueError, KeyError) as exc:
            self.backoff.record_block(store_id, f"formato inesperado: {exc}")
            return None
        self.backoff.record_success(store_id)
        return item


default_direct_reader = DirectStoreReader()
