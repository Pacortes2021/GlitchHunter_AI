"""
Bot de Telegram y Despacho Multicanal de Alertas Instantáneas para GlitchHunter AI.

Subagente 13: Responsable de formatear alertas de alta conversión para compradores y
cazadores de ofertas, generar botones inline interactivos de compra directa y enlace al
dashboard, despachar notificaciones asíncronas a la API de Telegram Bot y gestionar
un modo de simulación de alta fidelidad para desarrollo local con trazabilidad en JSONL.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Union

# Configuración de rutas para imports robustos
_CURRENT_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _CURRENT_DIR.parent
_WORKSPACE_DIR = _PROJECT_DIR.parent

for p in (str(_PROJECT_DIR), str(_WORKSPACE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from glitchhunter_ai import config
except ImportError:
    try:
        import config
    except ImportError:
        config = None

try:
    from glitchhunter_ai.core.models import (
        GlitchAlert,
        GlitchSeverity,
        GlitchVerdict,
        ProductItem,
        SemanticClassificationResult,
        TelegramAlertPayload,
    )
except ImportError:
    from core.models import (
        GlitchAlert,
        GlitchSeverity,
        GlitchVerdict,
        ProductItem,
        SemanticClassificationResult,
        TelegramAlertPayload,
    )

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import requests
except ImportError:
    requests = None

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

logger = logging.getLogger("glitchhunter.notifications.telegram")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class AlertDispatchResult(int):
    """
    Resultado de despacho compatible con contextos síncronos y asíncronos.
    En contexto síncrono actúa directamente como un booleano (True/False / 1/0).
    En contexto asíncrono permite ser utilizado con `await dispatcher.send_alert(...)`.
    """
    def __new__(cls, value: bool, coro_func=None):
        instance = super().__new__(cls, 1 if value else 0)
        instance._val = bool(value)
        instance._coro_func = coro_func
        return instance

    def __bool__(self) -> bool:
        return self._val

    def __repr__(self) -> str:
        return f"AlertDispatchResult({self._val})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return self._val is other or self._val == other
        return super().__eq__(other)

    def __await__(self):
        if self._coro_func:
            return self._coro_func().__await__()
        async def _dummy():
            return self._val
        return _dummy().__await__()


class TelegramAlertDispatcher:
    """
    Despachador multicanal de alertas de glitches de precio vía Telegram Bot API.

    Funcionalidades principales:
      - Formateo profesional con emojis y métricas clave (descuento, ahorro CLP, certeza IA).
      - Generación de teclado interactivo con botones inline de compra y dashboard.
      - Modo Simulación con visualización enriquecida en terminal (Rich) y persistencia en JSONL.
      - Envío asíncrono en vivo mediante Telegram Bot API (`sendMessage` con `InlineKeyboardMarkup`).
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        simulation_mode: Optional[bool] = None,
        log_file: Optional[Union[str, Path]] = None,
        web_host: Optional[str] = None,
        web_port: Optional[int] = None,
    ):
        """
        Inicializa el despachador de Telegram.

        Parámetros:
            bot_token: Token de autenticación del bot de Telegram.
            chat_id: Canal o ID de chat de destino (ej. @glitchhunter_chile_alerts).
            simulation_mode: Si es True, no contacta los servidores de Telegram y simula el despacho.
            log_file: Ruta al archivo JSONL donde se registran los envíos.
            web_host: Host del dashboard para enlaces interactivos.
            web_port: Puerto del servidor web.
        """
        # Carga desde config con fallbacks seguros
        self.bot_token = bot_token or (getattr(config, "TELEGRAM_BOT_TOKEN", None) if config else "mock_bot_token_glitchhunter")
        self.chat_id = chat_id or (getattr(config, "TELEGRAM_CHAT_ID", None) if config else "@glitchhunter_chile_alerts")
        
        cfg_sim = getattr(config, "TELEGRAM_SIMULATION_MODE", True) if config else True
        self.simulation_mode = simulation_mode if simulation_mode is not None else cfg_sim

        data_dir = getattr(config, "DATA_DIR", _PROJECT_DIR / "data") if config else _PROJECT_DIR / "data"
        if isinstance(data_dir, str):
            data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        if log_file:
            self.log_file = Path(log_file)
        else:
            self.log_file = data_dir / "telegram_sent_alerts.jsonl"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        self.web_host = web_host or (getattr(config, "WEB_HOST", "127.0.0.1") if config else "127.0.0.1")
        self.web_port = web_port or (getattr(config, "WEB_PORT", 8080) if config else 8080)

    @property
    def is_simulation(self) -> bool:
        """Determina si las alertas deben despacharse en modo simulación local."""
        if self.simulation_mode:
            return True
        if not self.bot_token or not self.bot_token.strip():
            return True
        if self.bot_token == "mock_bot_token_glitchhunter":
            return True
        if not self.chat_id or not self.chat_id.strip():
            return True
        return False

    def format_alert_message(self, alert: GlitchAlert) -> str:
        """
        Genera un mensaje formateado con emojis, diseño profesional tipo canal de ofertas premium.
        Usa formato Markdown estándar (con ** y *) compatible con visualización directa.

        Estructura:
          - 🚨 **ERROR DE PRECIO DETECTADO [SEVERIDAD]**
          - 🏪 **Tienda:** {store_name}
          - 📦 **Producto:** {title}
          - 🏷️ **Precio Glitch:** ${current_price:,.0f} CLP
          - ❌ **Precio Normal:** ${normal_price:,.0f} CLP
          - 🔥 **Descuento:** {discount_percentage:.1f}%
          - 💰 **Ahorro Directo:** ${savings:,.0f} CLP
          - 🧠 **Certeza IA:** {confidence_score:.0f}% ({reason})
          - ⚠️ *Aviso de stock limitado. Comprar antes de corrección manual del retailer.*
        """
        product = alert.product
        verdict = alert.verdict

        severity = verdict.severity.value if hasattr(verdict.severity, "value") else str(verdict.severity)
        store_name = product.store_name
        title = product.title
        current_price = product.best_price
        normal_price = product.normal_price
        discount_percentage = verdict.discount_percentage if verdict.discount_percentage is not None else product.discount_percentage
        savings = verdict.absolute_savings_clp if verdict.absolute_savings_clp is not None else product.absolute_savings_clp
        confidence_score = verdict.confidence_score
        reason = verdict.reason

        lines = [
            f"🚨 **ERROR DE PRECIO DETECTADO [{severity}]**",
            "",
            f"🏪 **Tienda:** {store_name}",
            f"📦 **Producto:** {title}",
            f"🏷️ **Precio Glitch:** ${current_price:,.0f} CLP",
            f"❌ **Precio Normal:** ${normal_price:,.0f} CLP",
            f"🔥 **Descuento:** {discount_percentage:.1f}%",
            f"💰 **Ahorro Directo:** ${savings:,.0f} CLP",
            f"🧠 **Certeza IA:** {confidence_score:.0f}% ({reason})",
        ]
        if getattr(alert, "market_median_price", None):
            match_str = f" por {alert.market_matched_model}" if alert.market_matched_model else (" vía SoloTodo" if alert.market_match_method == "solotodo" else "")
            lines.append(f"🌐 **Ref. Mercado:** ${alert.market_median_price:,.0f} CLP ({alert.market_stores_count} tiendas{match_str})".replace(",", "."))
        lines.extend([
            "",
            "⚠️ *Aviso de stock limitado. Comprar antes de corrección manual del retailer.*",
        ])

        return "\n".join(lines)

    def format_alert_html(self, alert: GlitchAlert) -> str:
        """
        Genera el mensaje con etiquetas HTML seguras para la API de Telegram (parse_mode='HTML').
        Escapa caracteres especiales del retailer/producto para prevenir errores de parsing.
        """
        product = alert.product
        verdict = alert.verdict

        severity = verdict.severity.value if hasattr(verdict.severity, "value") else str(verdict.severity)
        store_name = html.escape(str(product.store_name))
        title = html.escape(str(product.title))
        current_price = product.best_price
        normal_price = product.normal_price
        discount_percentage = verdict.discount_percentage if verdict.discount_percentage is not None else product.discount_percentage
        savings = verdict.absolute_savings_clp if verdict.absolute_savings_clp is not None else product.absolute_savings_clp
        confidence_score = verdict.confidence_score
        reason = html.escape(str(verdict.reason))

        lines = [
            f"🚨 <b>ERROR DE PRECIO DETECTADO [{severity}]</b>",
            "",
            f"🏪 <b>Tienda:</b> {store_name}",
            f"📦 <b>Producto:</b> {title}",
            f"🏷️ <b>Precio Glitch:</b> ${current_price:,.0f} CLP",
            f"❌ <b>Precio Normal:</b> ${normal_price:,.0f} CLP",
            f"🔥 <b>Descuento:</b> {discount_percentage:.1f}%",
            f"💰 <b>Ahorro Directo:</b> ${savings:,.0f} CLP",
            f"🧠 <b>Certeza IA:</b> {confidence_score:.0f}% ({reason})",
        ]
        if getattr(alert, "market_median_price", None):
            match_str = f" por {html.escape(str(alert.market_matched_model))}" if alert.market_matched_model else (" vía SoloTodo" if alert.market_match_method == "solotodo" else "")
            lines.append(f"🌐 <b>Ref. Mercado:</b> ${alert.market_median_price:,.0f} CLP ({alert.market_stores_count} tiendas{match_str})".replace(",", "."))
        lines.extend([
            "",
            "⚠️ <i>Aviso de stock limitado. Comprar antes de corrección manual del retailer.</i>",
        ])

        return "\n".join(lines)

    def build_inline_keyboard(self, alert: GlitchAlert) -> Dict[str, Any]:
        """
        Genera los botones inline interactivos de Telegram:
          - [🛒 AGREGAR AL CARRO (1 CLIC)] (Si está disponible en modo Sniper)
          - [⚡ COMPRAR AHORA (ENLACE DIRECTO)]
          - [📊 Ver Historial en Dashboard]
        """
        buy_url = alert.alert_url or alert.product.url
        dashboard_url = f"http://{self.web_host}:{self.web_port}/#alert-{alert.alert_id}"

        buttons = []
        if getattr(alert, "add_to_cart_url", None) and alert.add_to_cart_url != buy_url:
            buttons.append([{"text": "🛒 AGREGAR AL CARRO (1 CLIC)", "url": alert.add_to_cart_url}])

        buttons.append([{"text": "⚡ COMPRAR AHORA (ENLACE DIRECTO)", "url": buy_url}])
        buttons.append([{"text": "📊 Ver Historial en Dashboard", "url": dashboard_url}])

        # Feedback interactivo del usuario (👍 Oferta Real / 👎 Falso Positivo)
        buttons.append([
            {"text": "👍 Oferta Real", "callback_data": f"fb:up:{alert.alert_id}"},
            {"text": "👎 Falso Positivo", "callback_data": f"fb:down:{alert.alert_id}"},
        ])

        return {"inline_keyboard": buttons}

    def create_payload(self, alert: GlitchAlert, parse_mode: str = "HTML") -> TelegramAlertPayload:
        """
        Construye la entidad Pydantic TelegramAlertPayload lista para el despacho.
        """
        text = self.format_alert_html(alert) if parse_mode == "HTML" else self.format_alert_message(alert)
        reply_markup = self.build_inline_keyboard(alert)

        return TelegramAlertPayload(
            chat_id=self.chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            photo_url=alert.product.image_url,
        )

    def _log_sent_alert(
        self,
        alert: GlitchAlert,
        mode: str = "SIMULATION",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Registra la alerta despachada de manera estructurada en data/telegram_sent_alerts.jsonl.
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "alert_id": alert.alert_id,
            "store_id": alert.product.store_id,
            "store_name": alert.product.store_name,
            "product_id": alert.product.id,
            "title": alert.product.title,
            "current_price": alert.product.best_price,
            "normal_price": alert.product.normal_price,
            "discount_percentage": alert.verdict.discount_percentage,
            "savings_clp": alert.verdict.absolute_savings_clp,
            "severity": alert.verdict.severity.value if hasattr(alert.verdict.severity, "value") else str(alert.verdict.severity),
            "confidence_score": alert.verdict.confidence_score,
            "target_chat": self.chat_id,
            "mode": mode,
            "buy_url": alert.alert_url or alert.product.url,
            "dashboard_url": f"http://{self.web_host}:{self.web_port}/#alert-{alert.alert_id}",
            "details": details or {},
        }

        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error(f"Error escribiendo en log JSONL {self.log_file}: {exc}")

    def _render_terminal_banner(self, alert: GlitchAlert, message_text: str, keyboard: Dict[str, Any]) -> None:
        """
        Muestra en terminal una tarjeta visual atractiva de la alerta en modo simulación.
        """
        buy_url = keyboard["inline_keyboard"][0][0]["url"]
        dash_url = keyboard["inline_keyboard"][1][0]["url"]
        chat_info = f"Canal Destino: {self.chat_id} | Modo: SIMULACIÓN LOCAL | ID: {alert.alert_id}"

        if _HAS_RICH:
            console = Console()
            content = f"{message_text}\n\n"
            content += "🔘 [bold white]Botones Inline Interactivos:[/bold white]\n"
            content += f"   ⚡ [cyan][COMPRAR AHORA (ENLACE DIRECTO)][/cyan] -> [link={buy_url}]{buy_url}[/link]\n"
            content += f"   📊 [cyan][Ver Historial en Dashboard][/cyan] -> [link={dash_url}]{dash_url}[/link]"

            panel = Panel(
                Text.from_markup(content),
                title="[bold red]🚨 TELEGRAM ALERT DISPATCHED (SIMULACIÓN)[/bold red]",
                subtitle=f"[dim cyan]{chat_info}[/dim cyan]",
                border_style="red",
                expand=False,
            )
            console.print()
            console.print(panel)
            console.print()
        else:
            border = "=" * 80
            print(f"\n{border}")
            print("🚨 TELEGRAM ALERT DISPATCHED (SIMULACIÓN)")
            print(border)
            print(message_text)
            print("-" * 80)
            print("🔘 Botones Inline:")
            print(f"   [⚡ COMPRAR AHORA (ENLACE DIRECTO)] -> {buy_url}")
            print(f"   [📊 Ver Historial en Dashboard]     -> {dash_url}")
            print(border)
            print(f"📡 {chat_info}\n")

    def send_alert(self, alert: GlitchAlert) -> AlertDispatchResult:
        """
        Despacha una alerta de glitch a Telegram.

        Comportamiento:
          - Si `is_simulation` es True: Simula el envío imprimiendo en terminal con formato visual
            y persistiendo en `data/telegram_sent_alerts.jsonl`.
          - Si hay token real: Ejecuta la petición HTTP asíncrona a la API de Telegram.
          - Compatible tanto con llamadas síncronas (`dispatcher.send_alert(...)`) como
            asíncronas (`await dispatcher.send_alert(...)`).
        """
        if self.is_simulation:
            msg = self.format_alert_message(alert)
            keyboard = self.build_inline_keyboard(alert)
            self._render_terminal_banner(alert, msg, keyboard)
            self._log_sent_alert(alert, mode="SIMULATION")
            alert.telegram_sent = True
            logger.info(f"✅ Alerta {alert.alert_id} simulada exitosamente en Telegram para {self.chat_id}")
            return AlertDispatchResult(True)

        # Si estamos en modo en vivo con token configurado:
        # Ejecutar el despacho síncrono para verificar el éxito real del envío HTTP
        success = self.send_alert_sync(alert)
        return AlertDispatchResult(success)

    async def send_alert_async(self, alert: GlitchAlert) -> bool:
        """
        Envía la alerta de forma asíncrona a la API de Telegram Bot.
        Endpoint: https://api.telegram.org/bot<token>/sendMessage
        """
        if self.is_simulation:
            msg = self.format_alert_message(alert)
            keyboard = self.build_inline_keyboard(alert)
            self._render_terminal_banner(alert, msg, keyboard)
            self._log_sent_alert(alert, mode="SIMULATION")
            alert.telegram_sent = True
            return True

        if aiohttp is None:
            logger.warning("aiohttp no está instalado; recurriendo a despacho síncrono en hilo.")
            return self.send_alert_sync(alert)

        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": self.format_alert_html(alert),
            "parse_mode": "HTML",
            "reply_markup": self.build_inline_keyboard(alert),
            "disable_web_page_preview": False,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=10.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, json=payload) as resp:
                    resp_json = await resp.json()
                    if resp.status == 200 and resp_json.get("ok"):
                        alert.telegram_sent = True
                        self._log_sent_alert(alert, mode="LIVE", details=resp_json)
                        logger.info(f"🚀 Alerta {alert.alert_id} enviada exitosamente a {self.chat_id} vía Telegram API")
                        return True
                    else:
                        desc = resp_json.get("description", "Error desconocido de Telegram")
                        logger.error(f"❌ Error al enviar a Telegram ({resp.status}): {desc}")
                        return False
        except Exception as exc:
            logger.error(f"❌ Excepción de red al enviar alerta a Telegram: {exc}")
            return False

    def send_alert_sync(self, alert: GlitchAlert) -> bool:
        """
        Versión estrictamente síncrona de envío para scripts bloqueantes y pipelines tradicionales.
        """
        if self.is_simulation:
            res = self.send_alert(alert)
            return bool(res)

        if requests:
            api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": self.format_alert_html(alert),
                "parse_mode": "HTML",
                "reply_markup": self.build_inline_keyboard(alert),
                "disable_web_page_preview": False,
            }
            try:
                resp = requests.post(api_url, json=payload, timeout=10.0)
                resp_json = resp.json()
                if resp.status_code == 200 and resp_json.get("ok"):
                    alert.telegram_sent = True
                    self._log_sent_alert(alert, mode="LIVE", details=resp_json)
                    logger.info(f"🚀 Alerta {alert.alert_id} enviada exitosamente (sync) a {self.chat_id}")
                    return True
                else:
                    logger.error(f"❌ Error HTTP {resp.status_code} desde Telegram: {resp.text}")
                    return False
            except Exception as exc:
                logger.error(f"❌ Excepción síncrona en requests: {exc}")
                return False

        # Fallback a asyncio.run
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.send_alert_async(alert))

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, self.send_alert_async(alert)).result()

    def get_sent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retorna las últimas alertas registradas en el log JSONL.
        """
        if not self.log_file.exists():
            return []

        alerts = []
        try:
            with open(self.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        alerts.append(json.loads(line))
        except Exception as exc:
            logger.error(f"Error leyendo {self.log_file}: {exc}")
            return []

        return alerts[-limit:]

    def clear_log(self) -> None:
        """Limpia el archivo de historial JSONL (útil para pruebas)."""
        if self.log_file.exists():
            self.log_file.unlink()


def create_sample_glitch_alert() -> GlitchAlert:
    """
    Crea una alerta de glitch simulada de alta fidelidad para pruebas del bot de Telegram.
    Simula un error de omisión de un cero (Missing Zero Bug) en un Smart TV OLED de alta gama en Ripley.
    """
    now = datetime.now(timezone.utc)
    product = ProductItem(
        id="RIP-2000390111",
        store_id="ripley",
        store_name="Ripley Chile",
        title='Smart TV LG OLED evo 65" 4K UHD ThinQ AI OLED65C3PSA',
        category="televisores",
        current_price=129990.0,
        normal_price=1299990.0,
        card_price=119990.0,
        url="https://simple.ripley.cl/lg-oled-evo-65-4k-uhd-oled65c3psa-2000390111p",
        image_url="https://home.ripley.cl/store/Attachment/WOP/D172/2000390111/2000390111_2.jpg",
        in_stock=True,
        timestamp=now,
    )

    semantic = SemanticClassificationResult(
        is_accessory=False,
        is_main_device=True,
        detected_item_type="televisor_oled",
        explanation="Televisor OLED 65 pulgadas de alta gama, no es soporte, control ni accesorio",
        confidence=0.99,
    )

    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=98.0,
        discount_percentage=product.discount_percentage,
        absolute_savings_clp=product.absolute_savings_clp,
        is_missing_zero=True,
        is_fake_inflation=False,
        semantic_check=semantic,
        reason="Missing Zero Bug confirmado (~10.0x de desfase). Descuento real masivo en equipo principal sin inflación previa.",
    )

    return GlitchAlert(
        alert_id="ALT-OLED-65C3",
        product=product,
        verdict=verdict,
        created_at=now,
        alert_url=product.url,
        telegram_sent=False,
    )


if __name__ == "__main__":
    print("=" * 80)
    print("🎯 Probando TelegramAlertDispatcher (Subagente 13 - GlitchHunter AI)")
    print("=" * 80)

    # 1. Instanciación del despachador
    dispatcher = TelegramAlertDispatcher()
    print(f"📡 Estado del Dispatcher:")
    print(f"   • Chat de Destino: {dispatcher.chat_id}")
    print(f"   • Modo Simulación: {dispatcher.is_simulation}")
    print(f"   • Archivo de Log:  {dispatcher.log_file}")
    print(f"   • Dashboard URL:   http://{dispatcher.web_host}:{dispatcher.web_port}")

    # 2. Creación de alerta de prueba
    test_alert = create_sample_glitch_alert()
    print(f"\n📦 Alerta de Prueba Preparada: {test_alert.alert_id}")
    print(f"   • Producto: {test_alert.product.title}")
    print(f"   • Precio Normal: ${test_alert.product.normal_price:,.0f} CLP")
    print(f"   • Precio Glitch: ${test_alert.product.best_price:,.0f} CLP")
    print(f"   • Ahorro:        ${test_alert.verdict.absolute_savings_clp:,.0f} CLP (-{test_alert.verdict.discount_percentage:.1f}%)")
    print(f"   • Certeza IA:    {test_alert.verdict.confidence_score:.0f}%")

    # 3. Formateo del mensaje
    formatted_msg = dispatcher.format_alert_message(test_alert)
    print(f"\n📝 Mensaje generado para Telegram:")
    print("-" * 50)
    print(formatted_msg)
    print("-" * 50)

    # 4. Botones interactivos inline
    buttons = dispatcher.build_inline_keyboard(test_alert)
    print(f"\n🔘 Botones Inline Generados:")
    for row in buttons["inline_keyboard"]:
        for btn in row:
            dest = btn.get("url") or btn.get("callback_data")
            print(f"   [{btn['text']}] -> {dest}")

    # 5. Envío simulado de la alerta
    print("\n🚀 Despachando alerta mediante dispatcher.send_alert()...")
    result = dispatcher.send_alert(test_alert)

    # 6. Verificaciones
    assert bool(result) is True, "El despacho debió retornar True"
    assert test_alert.telegram_sent is True, "La alerta debió marcar telegram_sent = True"
    assert dispatcher.log_file.exists(), f"El archivo {dispatcher.log_file} debió ser creado"

    sent_records = dispatcher.get_sent_alerts(limit=5)
    print(f"\n📊 Verificación de Persistencia JSONL:")
    print(f"   • Registros encontrados en {dispatcher.log_file.name}: {len(sent_records)}")
    if sent_records:
        latest = sent_records[-1]
        print(f"   • Última alerta registrada: ID={latest['alert_id']} | Tienda={latest['store_name']} | Modo={latest['mode']}")
        print(f"   • Ahorro Registrado: ${latest['savings_clp']:,.0f} CLP")

    print("\n" + "=" * 80)
    print("✅ TelegramAlertDispatcher completó todas las pruebas locales con éxito.")
    print("=" * 80)
