"""
Pruebas unitarias para TelegramAlertDispatcher (Subagente 13 - GlitchHunter AI).
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest

from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    ProductItem,
    SemanticClassificationResult,
    TelegramAlertPayload,
)
from glitchhunter_ai.notifications.telegram_bot import TelegramAlertDispatcher, create_sample_glitch_alert


@pytest.fixture
def sample_alert() -> GlitchAlert:
    now = datetime.now(timezone.utc)
    product = ProductItem(
        id="FAL-12345",
        store_id="falabella",
        store_name="Falabella",
        title="Notebook Gamer ASUS ROG Strix SCAR 16",
        category="computacion",
        current_price=249990.0,
        normal_price=2499990.0,
        card_price=229990.0,
        url="https://falabella.com/cl/p/asus-rog-scar-16",
        image_url="https://falabella.scene7.com/is/image/Falabella/12345",
        in_stock=True,
        timestamp=now,
    )
    verdict = GlitchVerdict(
        is_glitch=True,
        severity=GlitchSeverity.CRITICO,
        confidence_score=97.0,
        discount_percentage=product.discount_percentage,
        absolute_savings_clp=product.absolute_savings_clp,
        is_missing_zero=True,
        is_fake_inflation=False,
        semantic_check=SemanticClassificationResult(
            is_accessory=False,
            is_main_device=True,
            detected_item_type="notebook_gamer",
            explanation="Notebook de alta gama verificado",
            confidence=0.98,
        ),
        reason="Missing zero bug verificado (~10x caída de precio) en equipo gamer de alto valor.",
    )
    return GlitchAlert(
        alert_id="ALT-ROG-16",
        product=product,
        verdict=verdict,
        created_at=now,
        alert_url=product.url,
        telegram_sent=False,
    )


@pytest.fixture
def tmp_dispatcher(tmp_path: Path) -> TelegramAlertDispatcher:
    log_file = tmp_path / "test_telegram_alerts.jsonl"
    return TelegramAlertDispatcher(
        bot_token="mock_bot_token_glitchhunter",
        chat_id="@test_glitch_channel",
        simulation_mode=True,
        log_file=log_file,
        web_host="127.0.0.1",
        web_port=8080,
    )


def test_dispatcher_init_and_simulation(tmp_dispatcher: TelegramAlertDispatcher):
    assert tmp_dispatcher.chat_id == "@test_glitch_channel"
    assert tmp_dispatcher.is_simulation is True
    assert tmp_dispatcher.web_port == 8080


def test_simulation_detection():
    # Token mock -> Simulación
    d1 = TelegramAlertDispatcher(bot_token="mock_bot_token_glitchhunter", simulation_mode=False)
    assert d1.is_simulation is True

    # Token vacío -> Simulación
    d2 = TelegramAlertDispatcher(bot_token="", simulation_mode=False)
    assert d2.is_simulation is True

    # Token None -> Simulación
    d3 = TelegramAlertDispatcher(bot_token=None, simulation_mode=False)
    assert d3.is_simulation is True

    # Token real con simulation_mode=True -> Simulación
    d4 = TelegramAlertDispatcher(bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", simulation_mode=True)
    assert d4.is_simulation is True

    # Token real con simulation_mode=False y chat_id válido -> LIVE
    d5 = TelegramAlertDispatcher(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        chat_id="@real_channel",
        simulation_mode=False,
    )
    assert d5.is_simulation is False


def test_format_alert_message(tmp_dispatcher: TelegramAlertDispatcher, sample_alert: GlitchAlert):
    msg = tmp_dispatcher.format_alert_message(sample_alert)

    # Verificaciones de estructura requerida
    assert "🚨 **ERROR DE PRECIO DETECTADO [CRITICO]**" in msg
    assert "🏪 **Tienda:** Falabella" in msg
    assert "📦 **Producto:** Notebook Gamer ASUS ROG Strix SCAR 16" in msg
    assert "🏷️ **Precio Glitch:** $229,990 CLP" in msg
    assert "❌ **Precio Normal:** $2,499,990 CLP" in msg
    assert "🔥 **Descuento:**" in msg
    assert "💰 **Ahorro Directo:** $2,270,000 CLP" in msg
    assert "🧠 **Certeza IA:** 97% (Missing zero bug" in msg
    assert "⚠️ *Aviso de stock limitado. Comprar antes de corrección manual del retailer.*" in msg


def test_format_alert_html(tmp_dispatcher: TelegramAlertDispatcher, sample_alert: GlitchAlert):
    html_msg = tmp_dispatcher.format_alert_html(sample_alert)

    assert "🚨 <b>ERROR DE PRECIO DETECTADO [CRITICO]</b>" in html_msg
    assert "🏪 <b>Tienda:</b> Falabella" in html_msg
    assert "⚠️ <i>Aviso de stock limitado. Comprar antes de corrección manual del retailer.</i>" in html_msg
    # No deben quedar ** o * sueltos de markdown
    assert "**" not in html_msg


def test_build_inline_keyboard(tmp_dispatcher: TelegramAlertDispatcher, sample_alert: GlitchAlert):
    kb = tmp_dispatcher.build_inline_keyboard(sample_alert)

    assert "inline_keyboard" in kb
    rows = kb["inline_keyboard"]
    assert len(rows) == 3

    # Botón 1: Comprar ahora
    btn1 = rows[0][0]
    assert btn1["text"] == "⚡ COMPRAR AHORA (ENLACE DIRECTO)"
    assert btn1["url"] == sample_alert.product.url

    # Botón 2: Dashboard
    btn2 = rows[1][0]
    assert btn2["text"] == "📊 Ver Historial en Dashboard"
    assert btn2["url"] == f"http://127.0.0.1:8080/#alert-{sample_alert.alert_id}"

    # Fila 3: Botones interactivos de Feedback 👍 / 👎
    feedback_row = rows[2]
    assert len(feedback_row) == 2
    assert feedback_row[0]["text"] == "👍 Oferta Real"
    assert feedback_row[0]["callback_data"] == f"fb:up:{sample_alert.alert_id}"
    assert feedback_row[1]["text"] == "👎 Falso Positivo"
    assert feedback_row[1]["callback_data"] == f"fb:down:{sample_alert.alert_id}"


def test_create_payload(tmp_dispatcher: TelegramAlertDispatcher, sample_alert: GlitchAlert):
    payload = tmp_dispatcher.create_payload(sample_alert, parse_mode="HTML")

    assert isinstance(payload, TelegramAlertPayload)
    assert payload.chat_id == "@test_glitch_channel"
    assert payload.parse_mode == "HTML"
    assert "<b>ERROR DE PRECIO DETECTADO" in payload.text
    assert payload.reply_markup is not None
    assert payload.photo_url == sample_alert.product.image_url


def test_send_alert_simulation_sync(tmp_dispatcher: TelegramAlertDispatcher, sample_alert: GlitchAlert):
    assert sample_alert.telegram_sent is False
    assert not tmp_dispatcher.log_file.exists()

    result = tmp_dispatcher.send_alert(sample_alert)

    # Verificación síncrona
    assert bool(result) is True
    assert result == True
    assert sample_alert.telegram_sent is True
    assert tmp_dispatcher.log_file.exists()

    history = tmp_dispatcher.get_sent_alerts()
    assert len(history) == 1
    record = history[0]
    assert record["alert_id"] == sample_alert.alert_id
    assert record["store_id"] == "falabella"
    assert record["mode"] == "SIMULATION"
    assert record["savings_clp"] == 2270000.0


def test_send_alert_simulation_async(tmp_dispatcher: TelegramAlertDispatcher, sample_alert: GlitchAlert):
    import asyncio

    sample_alert.telegram_sent = False
    tmp_dispatcher.clear_log()

    # Invocación asíncrona mediante await send_alert
    async def _runner():
        return await tmp_dispatcher.send_alert(sample_alert)

    result = asyncio.run(_runner())

    assert result is True
    assert sample_alert.telegram_sent is True
    history = tmp_dispatcher.get_sent_alerts()
    assert len(history) == 1


def test_create_sample_glitch_alert_helper():
    alert = create_sample_glitch_alert()
    assert isinstance(alert, GlitchAlert)
    assert alert.verdict.severity == GlitchSeverity.CRITICO
    assert alert.product.store_id == "ripley"
    assert alert.verdict.is_missing_zero is True
    assert alert.verdict.confidence_score >= 95.0
