"""
Módulo de Notificaciones y Despacho de Alertas para GlitchHunter AI.
Incluye integraciones con Telegram Bot API, notificaciones push móviles ntfy y webhooks.
"""

from glitchhunter_ai.notifications.telegram_bot import TelegramAlertDispatcher
from glitchhunter_ai.notifications.ntfy_client import NtfyAlertDispatcher

__all__ = ["TelegramAlertDispatcher", "NtfyAlertDispatcher"]
