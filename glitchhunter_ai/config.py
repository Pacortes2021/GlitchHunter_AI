"""
Configuración global de GlitchHunter AI.
Define rutas, umbrales de detección y configuración de tiendas.
"""

from pathlib import Path
import os

try:
    from dotenv import load_dotenv
    # Cargar automáticamente variables de entorno desde .env si existe
    BASE_DIR = Path(__file__).resolve().parent
    for env_file in [BASE_DIR / ".env", BASE_DIR.parent / ".env"]:
        if env_file.is_file():
            load_dotenv(dotenv_path=env_file)
            break
    else:
        load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("GLITCHHUNTER_DATA_DIR") or BASE_DIR / "data")  # Sobrescribible para aislar tests
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "glitchhunter.db"

# Umbrales de Detección de Glitch
MIN_DISCOUNT_PERCENTAGE = float(os.getenv("MIN_DISCOUNT_PERCENTAGE", "65.0"))       # Porcentaje mínimo de descuento para sospecha
CRITICAL_DISCOUNT_PERCENTAGE = float(os.getenv("CRITICAL_DISCOUNT_PERCENTAGE", "85.0"))  # Umbral para alerta crítica (urgente)
MIN_ABSOLUTE_SAVINGS_CLP = float(os.getenv("MIN_ABSOLUTE_SAVINGS_CLP", "30000.0"))   # Ahorro mínimo en pesos chilenos ($30.000)

# Canal de ofertas comerciales (40%-64%): filtro de calidad antes de publicar
DEAL_MIN_SAVINGS_CLP = float(os.getenv("DEAL_MIN_SAVINGS_CLP", "0"))         # Ahorro mínimo de una oferta (0 = sin mínimo)
DEAL_PUBLISH_BATCH = int(os.getenv("DEAL_PUBLISH_BATCH", "3000"))              # Candidatas revisadas por ciclo
DEAL_MAX_AGE_HOURS = float(os.getenv("DEAL_MAX_AGE_HOURS", "12"))            # Solo productos vistos recientemente
# Una publicación pasa a "sin confirmar" si ningún escaneo ve el producto en estas horas
FEED_NOT_SEEN_HOURS = float(os.getenv("FEED_NOT_SEEN_HOURS", "24"))
DEAL_INCLUDE_NON_TECH = os.getenv("DEAL_INCLUDE_NON_TECH", "true").lower() in ("true", "1", "yes")  # Ropa, muebles, etc.
# Tiendas "solo errores de precio" (opcional, vía .env): sus liquidaciones normales no se publican
# como ofertas; solo entran al canal los errores de precio verificados. Por defecto todas publican ofertas.
DEAL_GLITCH_ONLY_STORES = {
    s.strip().lower() for s in os.getenv(
        "DEAL_GLITCH_ONLY_STORES",
        "",
    ).split(",") if s.strip()
}

# Criterio de "Pérdida de Ceros" (Missing Zero Bug)
# Ej: Producto de $899.990 publicado a $89.990 (ratio ~10x)
MISSING_ZERO_RATIO_MIN = float(os.getenv("MISSING_ZERO_RATIO_MIN", "8.5"))
MISSING_ZERO_RATIO_MAX = float(os.getenv("MISSING_ZERO_RATIO_MAX", "11.5"))
# Bajo este precio normal un ~10x no es un cero perdido sino una liquidación de productos baratos
# (DBS: muestras de $4.490 a $440)
MISSING_ZERO_MIN_REFERENCE_CLP = float(os.getenv("MISSING_ZERO_MIN_REFERENCE_CLP", "20000"))

# Configuración de Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "mock_bot_token_glitchhunter")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@glitchhunter_chile_alerts")
TELEGRAM_SIMULATION_MODE = os.getenv("TELEGRAM_SIMULATION_MODE", "true").lower() in ("true", "1", "yes")

# Configuración de Notificaciones Push Móviles vía ntfy (https://ntfy.sh)
# Los topics de ntfy.sh son públicos: cualquiera que conozca el nombre puede leerlos o publicar.
# No hay topic por defecto; define uno largo y aleatorio en .env (ntfy queda desactivado sin él).
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_ENABLED = os.getenv("NTFY_ENABLED", "true").lower() in ("true", "1", "yes") and bool(NTFY_TOPIC)

# Configuración del Servidor Web
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
API_AUTH_KEY = os.getenv("API_AUTH_KEY", "")  # Token de acceso opcional para endpoints administrativos (/api/scan, etc.)
# Orígenes permitidos (ej: "http://localhost:3000,https://midominio.com"). Por defecto solo el propio dashboard.
CORS_ORIGINS = os.getenv("CORS_ORIGINS", f"http://127.0.0.1:{WEB_PORT},http://localhost:{WEB_PORT}")
ALLOW_TEST_GLITCH = os.getenv("ALLOW_TEST_GLITCH", "true").lower() in ("true", "1", "yes")

# Configuración de Discord Webhook
DISCORD_ENABLED = os.getenv("DISCORD_ENABLED", "true").lower() in ("true", "1", "yes")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Intervalo del Escaneo Automático en Segundo Plano (en segundos)
# 300s = cada 5 minutos: los agregadores actualizan cada ~40 min, escanear más seguido no agrega datos
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
AUTO_SCAN_ENABLED = os.getenv("AUTO_SCAN_ENABLED", "true").lower() in ("true", "1", "yes")
USE_LIVE_SCRAPING = os.getenv("USE_LIVE_SCRAPING", "true").lower() in ("true", "1", "yes")

# Modo híbrido: lectura directa de Falabella y Ripley (precios en tiempo real, sin el retraso del agregador)
# DIRECT_CONFIRM_ENABLED: confirma contra la tienda cada candidato detectado vía agregador antes de alertar.
# DIRECT_SCAN_ENABLED: escaneo directo frecuente de la watchlist y de DIRECT_SCAN_CATEGORIES.
# Cada página de la tienda pesa ~1-2,5 MB: mantén pocas categorías y un intervalo razonable.
DIRECT_CONFIRM_ENABLED = os.getenv("DIRECT_CONFIRM_ENABLED", "true").lower() in ("true", "1", "yes")
DIRECT_SCAN_ENABLED = os.getenv("DIRECT_SCAN_ENABLED", "true").lower() in ("true", "1", "yes")
DIRECT_SCAN_INTERVAL_SECONDS = int(os.getenv("DIRECT_SCAN_INTERVAL_SECONDS", "180"))
DIRECT_SCAN_CATEGORIES = [c.strip() for c in os.getenv("DIRECT_SCAN_CATEGORIES", "").split(",") if c.strip()]
# PC Factory: páginas de 48 productos de su catálogo completo leídas por ciclo (~35 KB c/u, ~63 páginas en total)
DIRECT_PCFACTORY_PAGES_PER_CYCLE = int(os.getenv("DIRECT_PCFACTORY_PAGES_PER_CYCLE", "10"))
# Tiendas consultadas a la vez en el escaneo directo (cada una es un sitio distinto)
DIRECT_FETCH_WORKERS = int(os.getenv("DIRECT_FETCH_WORKERS", "16"))
# Si el escaneo directo se omitió por un escaneo completo en curso, se reintenta a los N segundos
DIRECT_RETRY_SECONDS = int(os.getenv("DIRECT_RETRY_SECONDS", "15"))

# Modo evento: en CyberDay, Cyber Monday, Black Friday y Navidad ocurre la mayoría de los errores
# de precio, y duran de minutos a 2 horas. Durante esas fechas el escaneo directo es más frecuente.
# EVENT_WINDOWS: "Nombre=AAAA-MM-DD:AAAA-MM-DD, …" (hora de Chile, fechas inclusive).
# EVENT_MODE: auto (según EVENT_WINDOWS) | on (siempre) | off (nunca).
EVENT_WINDOWS = os.getenv("EVENT_WINDOWS", "")
EVENT_MODE = os.getenv("EVENT_MODE", "auto")
EVENT_DIRECT_SCAN_INTERVAL_SECONDS = int(os.getenv("EVENT_DIRECT_SCAN_INTERVAL_SECONDS", "60"))

# Tiendas soportadas en Chile
STORES = [
    {"id": "falabella", "name": "Falabella", "domain": "falabella.com"},
    {"id": "ripley", "name": "Ripley", "domain": "simple.ripley.cl"},
    {"id": "paris", "name": "Paris", "domain": "paris.cl"},
    {"id": "lider", "name": "Líder", "domain": "lider.cl"},
    {"id": "mercadolibre", "name": "Mercado Libre", "domain": "mercadolibre.cl"},
    {"id": "pcfactory", "name": "PC Factory", "domain": "pcfactory.cl"},
    {"id": "sodimac", "name": "Sodimac", "domain": "sodimac.cl"},
    {"id": "abc", "name": "ABC", "domain": "abc.cl"},
    {"id": "jumbo", "name": "Jumbo", "domain": "jumbo.cl"},
    {"id": "hp", "name": "HP", "domain": "hp.com"},
    {"id": "winpy", "name": "Winpy", "domain": "winpy.cl"},
    {"id": "spdigital", "name": "SP Digital", "domain": "spdigital.cl"},
    {"id": "santaisabel", "name": "Santa Isabel", "domain": "santaisabel.cl"},
    {"id": "farmaciamapuche", "name": "Farmacia Mapuche", "domain": "farmaciamapuche.cl"},
    {"id": "rosen", "name": "Rosen", "domain": "rosen.cl"},
    {"id": "cannonhome", "name": "Cannon Home", "domain": "cannonhome.cl"},
    {"id": "santarita", "name": "Santa Rita", "domain": "santaritaonline.com"},
    {"id": "petplanet", "name": "Pet Planet", "domain": "petplanet.cl"},
    {"id": "toyng", "name": "Toyng", "domain": "toyng.cl"},
    {"id": "juanvaldez", "name": "Juan Valdez", "domain": "juanvaldez.cl"},
    {"id": "promusic", "name": "ProMusic", "domain": "promusic.cl"},
    {"id": "sony", "name": "Sony Store", "domain": "store.sony.cl"},
    {"id": "motorola", "name": "Motorola", "domain": "motorola.cl"},
    {"id": "electrolux", "name": "Electrolux", "domain": "electrolux.cl"},
    {"id": "fensa", "name": "Fensa", "domain": "tiendafensa.cl"},
    {"id": "oster", "name": "Oster", "domain": "oster.cl"},
    {"id": "casaroyal", "name": "Casa Royal", "domain": "casaroyal.cl"},
    {"id": "nike", "name": "Nike", "domain": "nike.cl"},
    {"id": "levis", "name": "Levi's", "domain": "levi.cl"},
    {"id": "reebok", "name": "Reebok", "domain": "reebok.cl"},
    {"id": "columbia", "name": "Columbia", "domain": "columbiachile.cl"},
    {"id": "caterpillar", "name": "Caterpillar", "domain": "shopcaterpillar.cl"},
    {"id": "patagonia", "name": "Patagonia", "domain": "cl.patagonia.com"},
    {"id": "underarmour", "name": "Under Armour", "domain": "underarmour.cl"},
    {"id": "crocs", "name": "Crocs", "domain": "crocs.cl"},
    {"id": "vans", "name": "Vans", "domain": "vans.cl"},
    {"id": "doite", "name": "Doite", "domain": "doite.cl"},
    {"id": "havaianas", "name": "Havaianas", "domain": "havaianas.cl"},
    {"id": "oxford", "name": "Oxford", "domain": "oxfordstore.cl"},
    {"id": "aufbau", "name": "Aufbau", "domain": "aufbau.cl"},
    {"id": "lippi", "name": "Lippi", "domain": "lippioutdoor.com"},
    {"id": "kitchencenter", "name": "Kitchen Center", "domain": "kitchencenter.cl"},
    {"id": "thenorthface", "name": "The North Face", "domain": "thenorthface.cl"},
    {"id": "converse", "name": "Converse", "domain": "converse.cl"},
    {"id": "lacoste", "name": "Lacoste", "domain": "lacoste.cl"},
    {"id": "marmot", "name": "Marmot", "domain": "marmot.cl"},
    {"id": "sparta", "name": "Sparta", "domain": "sparta.cl"},
    {"id": "dbs", "name": "DBS", "domain": "dbs.cl"},
    {"id": "mimbral", "name": "Mimbral", "domain": "mimbral.cl"},
    {"id": "audiomusica", "name": "Audiomúsica", "domain": "audiomusica.com"},
    {"id": "dimeiggs", "name": "Dimeiggs", "domain": "dimeiggs.cl"},
    {"id": "drsimi", "name": "Dr. Simi", "domain": "drsimi.cl"},
    {"id": "mademsa", "name": "Mademsa", "domain": "tiendamademsa.cl"},
    {"id": "dimarsa", "name": "Dimarsa", "domain": "dimarsa.cl"},
    {"id": "marsella", "name": "Ferretería Marsella", "domain": "ferreteriamarsella.cl"},
    {"id": "construplaza", "name": "Construplaza", "domain": "construplaza.cl"},
    {"id": "tiendasalfa", "name": "Tiendas Alfa", "domain": "tiendasalfa.cl"},
    {"id": "tramontina", "name": "Tramontina", "domain": "tramontina.cl"},
    {"id": "dimarine", "name": "Dimarine", "domain": "dimarine.cl"},
    {"id": "lavinoteca", "name": "La Vinoteca", "domain": "lavinoteca.cl"},
    {"id": "maxservice", "name": "Maxservice", "domain": "maxservice.cl"},
    {"id": "ventus", "name": "Ventus", "domain": "ventuscorp.cl"},
    {"id": "karcher", "name": "Kärcher", "domain": "karcherstore.cl"},
    {"id": "esika", "name": "Ésika", "domain": "esika.tiendabelcorp.cl"},
    {"id": "lbel", "name": "L'Bel", "domain": "lbel.tiendabelcorp.cl"},
    {"id": "clubsoftys", "name": "Club Softys", "domain": "clubsoftys.cl"},
    {"id": "coleman", "name": "Coleman", "domain": "coleman.cl"},
    {"id": "marleycoffee", "name": "Marley Coffee", "domain": "marleycoffee.cl"},
    {"id": "bbqgrill", "name": "BBQ Grill", "domain": "bbqgrill.cl"},
    {"id": "blanik", "name": "Blanik", "domain": "blanik.cl"},
    {"id": "cotidian", "name": "Cotidian", "domain": "mitiendacotidian.cl"},
    {"id": "colloky", "name": "Colloky", "domain": "colloky.cl"},
    {"id": "ficcus", "name": "Ficcus", "domain": "ficcus.cl"},
    {"id": "zapatos", "name": "Zapatos.cl", "domain": "zapatos.cl"},
    {"id": "hm", "name": "H&M", "domain": "cl.hm.com"},
    {"id": "dolly", "name": "Dolly", "domain": "dolly.cl"},
    {"id": "ferouch", "name": "Ferouch", "domain": "ferouch.cl"},
    {"id": "calvinklein", "name": "Calvin Klein", "domain": "calvinklein.cl"},
    {"id": "theline", "name": "The Line", "domain": "theline.cl"},
    {"id": "tommy", "name": "Tommy Hilfiger", "domain": "cl.tommy.com"},
    {"id": "treck", "name": "Treck", "domain": "treck.cl"},
    {"id": "velez", "name": "Vélez", "domain": "velez.cl"},
    {"id": "privilege", "name": "Privilege", "domain": "privilege.cl"},
    {"id": "newman", "name": "Newman", "domain": "newman.cl"},
    {"id": "swarovski", "name": "Swarovski", "domain": "swarovski.cl"},
    {"id": "womensecret", "name": "Women'secret", "domain": "womensecret.cl"},
    {"id": "totto", "name": "Totto", "domain": "cl.totto.com"},
    {"id": "oneill", "name": "O'Neill", "domain": "oneill.cl"},
    {"id": "americaneagle", "name": "American Eagle", "domain": "ae.cl"},
    {"id": "asics", "name": "Asics", "domain": "asics.cl"},
    {"id": "trial", "name": "Trial", "domain": "trial.cl"},
    {"id": "umbrale", "name": "Umbrale", "domain": "umbrale.cl"},
    {"id": "mountainhardwear", "name": "Mountain Hardwear", "domain": "mountainhardwear.cl"},
    {"id": "ilgioco", "name": "Il Gioco", "domain": "ilgioco.cl"},
    {"id": "sharkninja", "name": "SharkNinja", "domain": "sharkninja.cl"},
    {"id": "dermotienda", "name": "Dermotienda", "domain": "dermotienda.cl"},
    {"id": "herramientastotal", "name": "Herramientas Total", "domain": "herramientastotal.cl"},
    {"id": "delonghi", "name": "De'Longhi", "domain": "delonghi.cl"},
    {"id": "antartica", "name": "Antártica", "domain": "antartica.cl"},
    {"id": "weplay", "name": "Weplay", "domain": "weplay.cl"},
    {"id": "sdn", "name": "SDN Neumáticos", "domain": "sdn.cl"},
    {"id": "novasalud", "name": "Novasalud", "domain": "novasalud.cl"},
    {"id": "ebest", "name": "eBest", "domain": "ebest.cl"},
    {"id": "bathandbodyworks", "name": "Bath & Body Works", "domain": "bathandbodyworks.cl"},
    {"id": "schilling", "name": "Schilling", "domain": "schilling.cl"},
    {"id": "flex", "name": "Flex", "domain": "flex.cl"},
    {"id": "headchile", "name": "Head", "domain": "headchile.com"},
    {"id": "epson", "name": "Tienda Epson", "domain": "tiendaepson.cl"},
    {"id": "umbro", "name": "Umbro", "domain": "umbro.cl"},
    {"id": "kotting", "name": "Kotting", "domain": "kotting.cl"},
    {"id": "roly", "name": "Roly", "domain": "roly.cl"},
    {"id": "surprice", "name": "Surprice", "domain": "surprice.cl"},
    {"id": "ash", "name": "Ash", "domain": "ashchile.cl"},
    {"id": "bananarepublic", "name": "Banana Republic", "domain": "bananarepublic.cl"},
    {"id": "brooksbrothers", "name": "Brooks Brothers", "domain": "brooksbrothers.cl"},
    {"id": "calubags", "name": "Calu Bags", "domain": "calubags.com"},
    {"id": "dcshoes", "name": "DC Shoes", "domain": "dcshoes.cl"},
    {"id": "globe", "name": "Globe", "domain": "globebrand.cl"},
    {"id": "guess", "name": "Guess", "domain": "guess.cl"},
    {"id": "jayson", "name": "Jayson", "domain": "jayson.cl"},
    {"id": "kannu", "name": "Kannu", "domain": "kannu.cl"},
    {"id": "kipling", "name": "Kipling", "domain": "kipling.cl"},
    {"id": "magriffe", "name": "Magriffe", "domain": "magriffe.cl"},
    {"id": "mammut", "name": "Mammut", "domain": "mammut.cl"},
    {"id": "mauiandsons", "name": "Maui and Sons", "domain": "mauiandsons.cl"},
    {"id": "mcgregor", "name": "McGregor", "domain": "mcgregor.cl"},
    {"id": "potros", "name": "Potros", "domain": "potros.cl"},
    {"id": "rapsodia", "name": "Rapsodia", "domain": "rapsodia.cl"},
    {"id": "ripcurl", "name": "Rip Curl", "domain": "ripcurl.cl"},
    {"id": "rusty", "name": "Rusty", "domain": "rusty.cl"},
    {"id": "stoked", "name": "Stoked", "domain": "stoked.cl"},
    {"id": "gap", "name": "Gap", "domain": "gap.cl"},
    {"id": "ugg", "name": "UGG", "domain": "ugg.cl"},
    {"id": "victoriassecret", "name": "Victoria's Secret", "domain": "victoriassecret.cl"},
    {"id": "volcom", "name": "Volcom", "domain": "volcom.cl"},
    {"id": "wa2", "name": "Wa2", "domain": "wa2.cl"},
    {"id": "solotodo", "name": "SoloTodo", "domain": "solotodo.cl"},
]
