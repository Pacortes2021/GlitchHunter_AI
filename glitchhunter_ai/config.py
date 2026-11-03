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
DEAL_MIN_SAVINGS_CLP = float(os.getenv("DEAL_MIN_SAVINGS_CLP", "10000"))     # Ahorro mínimo de una oferta
DEAL_MAX_AGE_HOURS = float(os.getenv("DEAL_MAX_AGE_HOURS", "12"))            # Solo productos vistos recientemente
DEAL_INCLUDE_NON_TECH = os.getenv("DEAL_INCLUDE_NON_TECH", "true").lower() in ("true", "1", "yes")  # Ropa, muebles, etc.

# Criterio de "Pérdida de Ceros" (Missing Zero Bug)
# Ej: Producto de $899.990 publicado a $89.990 (ratio ~10x)
MISSING_ZERO_RATIO_MIN = float(os.getenv("MISSING_ZERO_RATIO_MIN", "8.5"))
MISSING_ZERO_RATIO_MAX = float(os.getenv("MISSING_ZERO_RATIO_MAX", "11.5"))

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

# Tiendas soportadas en Chile
STORES = [
    {"id": "falabella", "name": "Falabella", "domain": "falabella.com"},
    {"id": "ripley", "name": "Ripley", "domain": "simple.ripley.cl"},
    {"id": "paris", "name": "Paris", "domain": "paris.cl"},
    {"id": "lider", "name": "Líder", "domain": "lider.cl"},
    {"id": "mercadolibre", "name": "Mercado Libre", "domain": "mercadolibre.cl"},
    {"id": "pcfactory", "name": "PC Factory", "domain": "pcfactory.cl"},
    {"id": "sodimac", "name": "Sodimac", "domain": "sodimac.cl"},
    {"id": "solotodo", "name": "SoloTodo", "domain": "solotodo.cl"},
]
