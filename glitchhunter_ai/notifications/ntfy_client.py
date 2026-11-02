"""
GlitchHunter AI - Despachador de Notificaciones Push Móviles vía ntfy (https://ntfy.sh)
Envía alertas prioritarias a tu teléfono móvil con enlace de apertura directa:
- Al tocar la notificación en tu celular, abre automáticamente la URL de compra.
- Botones de acción directa integrados ("Comprar Ahora" y "Ver en Radar").
- Prioridad 'urgent' para activar sonido/vibración inmediata en el teléfono.
"""

import asyncio
import logging
import urllib.parse
from typing import Any, Dict, Optional

import requests

from glitchhunter_ai.config import (
    NTFY_ENABLED,
    NTFY_SERVER,
    NTFY_TOPIC,
    WEB_HOST,
    WEB_PORT,
)
from glitchhunter_ai.core.models import GlitchAlert, GlitchSeverity

logger = logging.getLogger("GlitchHunter.Ntfy")


class NtfyAlertDispatcher:
    """
    Despachador de notificaciones push móviles hacia la aplicación ntfy.
    """

    def __init__(
        self,
        server_url: str = NTFY_SERVER,
        topic: str = NTFY_TOPIC,
        enabled: bool = NTFY_ENABLED,
        simulation_mode: bool = False,
        timeout: int = 8,
    ):
        self.server_url = server_url.rstrip("/")
        self.topic = topic
        self.enabled = enabled
        self.simulation_mode = simulation_mode
        self.timeout = timeout
        self.endpoint = f"{self.server_url}/{self.topic}"

    def format_title(self, alert: GlitchAlert) -> str:
        """Genera el título conciso y llamativo para la notificación en pantalla bloqueada."""
        p = alert.product
        v = alert.verdict
        sev_tag = "[CRITICO]" if v.severity == GlitchSeverity.CRITICO else "[GLITCH]"
        return f"{sev_tag} {p.store_name}: -{v.discount_percentage:.0f}% OFF"

    def format_body(self, alert: GlitchAlert) -> str:
        """Genera el cuerpo del mensaje push con precios formateados en pesos chilenos."""
        p = alert.product
        v = alert.verdict
        best_price = p.card_price or p.current_price

        body_lines = [
            f"📦 {p.title}",
            f"🏷️ Precio: ${best_price:,.0f} CLP (Normal: ${p.normal_price:,.0f} CLP)".replace(",", "."),
            f"💰 Ahorras: ${v.absolute_savings_clp:,.0f} CLP".replace(",", "."),
        ]

        if v.is_missing_zero:
            body_lines.append("⚡ Falta de un cero detectada (~10x de caída)")

        body_lines.append(f"🧠 Dictamen IA: {v.reason}")
        return "\n".join(body_lines)

    def send_alert(self, alert: GlitchAlert) -> bool:
        """
        Envía la alerta push a tu teléfono de forma síncrona.
        Al tocar la notificación en tu móvil se abre de inmediato la URL del producto.
        En modo simulación o para alertas de test, no emite peticiones HTTP reales.
        """
        if not self.enabled:
            logger.debug("Notificaciones ntfy desactivadas en configuración.")
            return False

        p = alert.product
        v = alert.verdict

        # Aislamiento estricto de simulación / tests: no enviar push real a celulares
        is_simulated = (
            self.simulation_mode
            or p.id.startswith("test-")
            or p.store_id.startswith("test_")
            or getattr(alert, "is_simulation", False)
            or getattr(p, "is_mock", False)
        )
        if is_simulated:
            logger.info("📱 [ntfy Simulación] Notificación push omitida para producto simulado: %s", p.title)
            return True
        title = self.format_title(alert)
        body = self.format_body(alert)

        # Prioridad alta para que despierte la pantalla del celular y emita sonido
        priority = "urgent" if v.severity == GlitchSeverity.CRITICO else "high"
        tags = "rotating_light,fire,moneybag" if v.severity == GlitchSeverity.CRITICO else "zap,shopping_trolley"

        # Headers HTTP para ntfy (Click abre automáticamente la URL al presionar la notificación)
        clean_title = title.encode("ascii", "ignore").decode("ascii")
        headers: Dict[str, str] = {
            "Title": clean_title,
            "Priority": priority,
            "Tags": tags,
            "Click": alert.alert_url or str(p.url),
            "Actions": (
                f"view, Comprar Ahora, {alert.alert_url or str(p.url)}; "
                f"view, Ver Radar, http://{WEB_HOST}:{WEB_PORT}/"
            ),
        }

        try:
            resp = requests.post(
                self.endpoint,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                logger.info(f"📱 Notificación ntfy enviada exitosamente al tema '{self.topic}' (ID: {alert.alert_id})")
                return True
            else:
                logger.warning(f"⚠️ Error al enviar a ntfy ({resp.status_code}): {resp.text}")
                return False
        except Exception as exc:
            logger.error(f"❌ Excepción conectando con ntfy.sh: {exc}")
            return False

    async def send_alert_async(self, alert: GlitchAlert) -> bool:
        """Versión asíncrona no bloqueante para endpoints y WebSockets."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.send_alert, alert)


if __name__ == "__main__":
    import uuid
    from datetime import datetime, timezone
    from glitchhunter_ai.core.models import (
        GlitchSeverity,
        GlitchVerdict,
        ProductItem,
        SemanticClassificationResult,
    )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    dispatcher = NtfyAlertDispatcher()

    # Producto de prueba
    sample_prod = ProductItem(
        id="test-ntfy-01",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Smart TV Samsung OLED 65\" 4K UHD QN65S90C + Barra Sonido",
        category="televisores",
        normal_price=999990.0,
        current_price=89990.0,
        card_price=79990.0,
        url="https://www.falabella.com/falabella-cl/product/16912389/Smart-TV-Samsung-OLED-65-4K/16912389",
    )

    sample_verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=99.0,
        discount_percentage=92.0,
        absolute_savings_clp=920000.0,
        is_missing_zero=True,
        is_fake_inflation=False,
        semantic_check=SemanticClassificationResult(
            is_accessory=False,
            is_main_device=True,
            detected_item_type="televisor",
            confidence=0.99,
            explanation="Smart TV OLED principal verificada.",
        ),
        reason="Missing Zero Bug (~10x) en Falabella Chile | Ahorro: $920.000 CLP",
    )

    sample_alert = GlitchAlert(
        alert_id=f"ALT-NTFY-{uuid.uuid4().hex[:6].upper()}",
        product=sample_prod,
        verdict=sample_verdict,
        alert_url=str(sample_prod.url),
        created_at=datetime.now(timezone.utc),
    )

    print(f"Probando envío de notificación a https://ntfy.sh/{dispatcher.topic} ...")
    success = dispatcher.send_alert(sample_alert)
    print("Resultado:", "Éxito ✅" if success else "Fallo ❌")
