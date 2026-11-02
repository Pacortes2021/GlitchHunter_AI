"""
Receptor de Feedback de Telegram (botones 👍 / 👎 de las alertas).
Lee las pulsaciones de los botones inline (callback_query) mediante long-polling a getUpdates,
registra el voto en la base de datos y responde a Telegram para cerrar el indicador de carga.
Solo se activa con un bot real (fuera del modo simulación).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("GlitchHunter.TelegramFeedback")

FEEDBACK_PREFIX = "fb:"


def parse_feedback_callback(data: str) -> Optional[tuple]:
    """Convierte 'fb:up:ALT-XXX' en ('up', 'ALT-XXX'). Retorna None si no es un voto válido."""
    if not data or not data.startswith(FEEDBACK_PREFIX):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in ("up", "down") or not parts[2]:
        return None
    return parts[1], parts[2]


class TelegramFeedbackPoller:
    """Procesa los votos enviados desde los botones inline de las alertas de Telegram."""

    def __init__(self, bot_token: str, db: Any, poll_timeout: int = 20):
        self.api_base = f"https://api.telegram.org/bot{bot_token}"
        self.db = db
        self.poll_timeout = poll_timeout
        self.offset: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _answer(self, callback_id: str, text: str) -> None:
        try:
            requests.post(
                f"{self.api_base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=5.0,
            )
        except Exception as exc:
            logger.debug(f"No se pudo responder callback {callback_id}: {exc}")

    def handle_update(self, update: Dict[str, Any]) -> None:
        """Registra el voto de una actualización callback_query y confirma al usuario."""
        cq = update.get("callback_query")
        if not cq:
            return
        parsed = parse_feedback_callback(cq.get("data", ""))
        if not parsed:
            self._answer(cq.get("id", ""), "Acción no reconocida")
            return
        vote, alert_id = parsed
        try:
            self.db.record_deal_feedback(deal_id=alert_id, vote=vote, reason="telegram")
            self._answer(cq["id"], "✅ Gracias, voto registrado" if vote == "up" else "✅ Marcado como falso positivo")
        except Exception as exc:
            logger.warning(f"Error registrando feedback de Telegram para {alert_id}: {exc}")
            self._answer(cq["id"], "⚠️ No se pudo registrar el voto")

    def poll_once(self) -> int:
        """Ejecuta una consulta getUpdates y procesa los votos recibidos. Retorna cuántos procesó."""
        params: Dict[str, Any] = {
            "timeout": self.poll_timeout,
            "allowed_updates": '["callback_query"]',
        }
        if self.offset is not None:
            params["offset"] = self.offset
        resp = requests.get(f"{self.api_base}/getUpdates", params=params, timeout=self.poll_timeout + 10)
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", f"HTTP {resp.status_code}"))

        updates: List[Dict[str, Any]] = payload.get("result", [])
        for update in updates:
            self.offset = update["update_id"] + 1
            self.handle_update(update)
        return len(updates)

    def _run(self) -> None:
        logger.info("📥 Receptor de votos de Telegram activo (botones 👍/👎).")
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                logger.warning(f"Error consultando votos de Telegram: {exc}")
                self._stop.wait(15)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-feedback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
