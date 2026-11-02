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
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "glitchhunter.db"

# Umbrales de Detección de Glitch
MIN_DISCOUNT_PERCENTAGE = float(os.getenv("MIN_DISCOUNT_PERCENTAGE", "65.0"))       # Porcentaje mínimo de descuento para sospecha
CRITICAL_DISCOUNT_PERCENTAGE = float(os.getenv("CRITICAL_DISCOUNT_PERCENTAGE", "85.0"))  # Umbral para alerta crítica (urgente)
MIN_ABSOLUTE_SAVINGS_CLP = float(os.getenv("MIN_ABSOLUTE_SAVINGS_CLP", "30000.0"))   # Ahorro mínimo en pesos chilenos ($30.000)

# Criterio de "Pérdida de Ceros" (Missing Zero Bug)
# Ej: Producto de $899.990 publicado a $89.990 (ratio ~10x)
MISSING_ZERO_RATIO_MIN = float(os.getenv("MISSING_ZERO_RATIO_MIN", "8.5"))
MISSING_ZERO_RATIO_MAX = float(os.getenv("MISSING_ZERO_RATIO_MAX", "11.5"))

# Configuración de Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "mock_bot_token_glitchhunter")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@glitchhunter_chile_alerts")
TELEGRAM_SIMULATION_MODE = os.getenv("TELEGRAM_SIMULATION_MODE", "true").lower() in ("true", "1", "yes")

# Configuración de Notificaciones Push Móviles vía ntfy (https://ntfy.sh)
NTFY_ENABLED = os.getenv("NTFY_ENABLED", "true").lower() in ("true", "1", "yes")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "glitchhunter_pablo_chile")

# Configuración del Servidor Web
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
API_AUTH_KEY = os.getenv("API_AUTH_KEY", "")  # Token de acceso opcional para endpoints administrativos (/api/scan, etc.)

# Configuración de Discord Webhook
DISCORD_ENABLED = os.getenv("DISCORD_ENABLED", "true").lower() in ("true", "1", "yes")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Intervalo del Escaneo Automático en Segundo Plano (en segundos)
# 60s = cada 1 minuto (solicitado para monitoreo rápido y oportuno)
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
AUTO_SCAN_ENABLED = os.getenv("AUTO_SCAN_ENABLED", "true").lower() in ("true", "1", "yes")
USE_LIVE_SCRAPING = os.getenv("USE_LIVE_SCRAPING", "true").lower() in ("true", "1", "yes")

# Tiendas soportadas en Chile
STORES = [
    {"id": "falabella", "name": "Falabella", "domain": "falabella.com"},
    {"id": "ripley", "name": "Ripley", "domain": "simple.ripley.cl"},
    {"id": "paris", "name": "Paris", "domain": "paris.cl"},
    {"id": "lider", "name": "Líder", "domain": "lider.cl"},
    {"id": "mercadolibre", "name": "Mercado Libre", "domain": "mercadolibre.cl"},
    {"id": "pcfactory", "name": "PC Factory", "domain": "pcfactory.cl"},
    {"id": "solotodo", "name": "SoloTodo", "domain": "solotodo.cl"},
]
