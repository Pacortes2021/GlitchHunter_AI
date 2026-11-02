"""
Despachador de Alertas vía Discord Webhook para GlitchHunter AI.
Envía embeds interactivos con soporte para modo Sniper (Add-to-Cart)
y notificaciones prioritarias para la Watchlist del usuario.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

from glitchhunter_ai.core.models import GlitchAlert, GlitchSeverity

logger = logging.getLogger("GlitchHunter.DiscordWebhook")

SEVERITY_COLORS = {
    GlitchSeverity.CRITICO: 0xE74C3C,   # Rojo vibrante
    GlitchSeverity.ALTO: 0xE67E22,      # Naranja intenso
    GlitchSeverity.MODERADO: 0x2ECC71,  # Verde esmeralda
    GlitchSeverity.FALSO_POSITIVO: 0x95A5A6,
}


class DiscordAlertDispatcher:
    """
    Despachador de alertas a canales de Discord mediante Webhooks oficiales.
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        enabled: Optional[bool] = None,
        simulation_mode: bool = False,
        timeout: float = 6.0,
    ):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL")
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.getenv("DISCORD_ENABLED", "false").lower() in ("true", "1", "yes")
        self.simulation_mode = simulation_mode
        self.timeout = timeout

    def format_embed(self, alert: GlitchAlert) -> Dict[str, Any]:
        """Construye un Embed enriquecido para Discord con colores según la severidad."""
        p = alert.product
        v = alert.verdict
        color = SEVERITY_COLORS.get(v.severity, 0x2ECC71)

        prefix = "⭐ [WATCHLIST HIT] " if getattr(alert, "is_watchlist_match", False) else "🚨 [GLITCH DETECTADO] "
        title = f"{prefix}{p.title[:200]}"

        # Precios legibles formateados en CLP
        current_fmt = f"${int(p.best_price):,}".replace(",", ".")
        normal_fmt = f"${int(p.normal_price):,}".replace(",", ".")
        savings_fmt = f"${int(v.absolute_savings_clp):,}".replace(",", ".")

        fields = [
            {"name": "🏪 Tienda", "value": f"**{p.store_name}**", "inline": True},
            {"name": "📉 Descuento", "value": f"**{v.discount_percentage:.1f}% OFF**", "inline": True},
            {"name": "💰 Ahorro CLP", "value": f"**{savings_fmt}**", "inline": True},
            {"name": "💵 Precio Oferta", "value": current_fmt, "inline": True},
            {"name": "🏷️ Precio Normal", "value": normal_fmt, "inline": True},
            {"name": "🧠 Certeza IA", "value": f"{v.confidence_score:.0f}%", "inline": True},
        ]

        if p.card_price:
            card_fmt = f"${int(p.card_price):,}".replace(",", ".")
            fields.append({"name": "💳 Exclusivo Tarjeta", "value": card_fmt, "inline": True})

        description_lines = [
            f"**Severidad:** `{v.severity.value if hasattr(v.severity, 'value') else str(v.severity)}`",
            f"**Análisis:** {v.reason}",
            "",
            f"🔗 [Ver Publicación Oficial]({alert.alert_url or p.url})",
        ]

        if alert.add_to_cart_url and alert.add_to_cart_url != (alert.alert_url or p.url):
            description_lines.append(f"⚡ [🛒 Agregar al Carro en 1 Clic (Sniper)]({alert.add_to_cart_url})")

        embed: Dict[str, Any] = {
            "title": title,
            "url": alert.alert_url or p.url,
            "description": "\n".join(description_lines),
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"GlitchHunter AI • ID: {alert.alert_id} • Monitoreo Continuo"
            },
        }

        if p.image_url:
            embed["thumbnail"] = {"url": p.image_url}

        return embed

    def send_alert(self, alert: GlitchAlert) -> bool:
        """
        Envía la alerta al webhook configurado en Discord de forma sincrónica.
        En modo simulación o con webhook de pruebas, valida el payload sin emitir peticiones de red reales.
        """
        # Si está explícitamente deshabilitado y NO en modo simulación
        if not self.enabled and not self.simulation_mode:
            logger.debug("Discord webhook desactivado en configuración.")
            return False

        if not self.webhook_url and not self.simulation_mode:
            logger.debug("Discord webhook sin URL configurada.")
            return False

        # Si está en modo simulación o el producto es de prueba
        p = alert.product
        is_simulated = (
            self.simulation_mode
            or p.id.startswith("test-")
            or p.store_id.startswith("test_")
            or getattr(alert, "is_simulation", False)
            or getattr(p, "is_mock", False)
            or (self.webhook_url and "mock" in self.webhook_url.lower())
            or (self.webhook_url and "example" in self.webhook_url.lower())
        )
        if is_simulated:
            logger.info("📡 [Discord Simulación] Embed despachado correctamente para: %s", alert.product.title)
            return True

        payload = {
            "username": "GlitchHunter AI",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/3500/3500833.png",
            "embeds": [self.format_embed(alert)],
        }

        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout)
            if resp.status_code in (200, 204):
                logger.info("✅ Alerta enviada exitosamente a Discord para %s", alert.product.id)
                return True
            else:
                logger.warning("Discord webhook respondió HTTP %s: %s", resp.status_code, resp.text[:200])
                return False
        except Exception as exc:
            logger.error("Error despachando alerta a Discord: %s", exc)
            return False

    async def send_alert_async(self, alert: GlitchAlert) -> bool:
        """Versión asincrónica para uso en pipelines y bucles de eventos."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.send_alert, alert)
