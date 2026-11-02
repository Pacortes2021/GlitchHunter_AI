"""
Configuración global de pytest.
Se ejecuta antes de importar los módulos de la aplicación para que la suite
nunca escriba en data/ real, nunca scrapee tiendas en vivo y nunca envíe
notificaciones push reales (ntfy, Discord, Telegram).
"""

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="glitchhunter_tests_")

os.environ["GLITCHHUNTER_DATA_DIR"] = _TEST_DATA_DIR
os.environ["USE_LIVE_SCRAPING"] = "false"
os.environ["AUTO_SCAN_ENABLED"] = "false"
os.environ["NTFY_ENABLED"] = "false"
os.environ["DISCORD_ENABLED"] = "false"
os.environ["TELEGRAM_SIMULATION_MODE"] = "true"
os.environ["API_AUTH_KEY"] = ""
