"""
Configuración global de pytest.
Se ejecuta antes de importar los módulos de la aplicación para que la suite
nunca escriba en data/ real, nunca scrapee tiendas en vivo y nunca envíe
notificaciones push reales (ntfy, Discord, Telegram).
"""

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="glitchhunter_tests_")

# Aislamiento estricto: impedir que .env local o variables de entorno contaminen los tests
os.environ["GLITCHHUNTER_SKIP_DOTENV"] = "1"
for _env_key in (
    "RADAR_ONLY_STORES",
    "RADAR_SKIP_STORES",
    "DEAL_GLITCH_ONLY_STORES",
    "EVENT_MODE",
    "EVENT_WINDOWS",
    "MIN_DISCOUNT_PERCENTAGE",
    "MIN_ABSOLUTE_SAVINGS_CLP",
    "DEAL_MIN_SAVINGS_CLP",
    "DEAL_PRIORITY_MIN_DISCOUNT",
    "DEAL_INCLUDE_NON_TECH",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
):
    os.environ.pop(_env_key, None)

os.environ["GLITCHHUNTER_DATA_DIR"] = _TEST_DATA_DIR
os.environ["USE_LIVE_SCRAPING"] = "false"
os.environ["AUTO_SCAN_ENABLED"] = "false"
os.environ["NTFY_ENABLED"] = "false"
os.environ["DISCORD_ENABLED"] = "false"
os.environ["TELEGRAM_SIMULATION_MODE"] = "true"
os.environ["API_AUTH_KEY"] = ""
os.environ["DIRECT_CONFIRM_ENABLED"] = "false"
os.environ["DIRECT_SCAN_ENABLED"] = "false"
