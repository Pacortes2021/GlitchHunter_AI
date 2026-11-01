"""
Pruebas unitarias para el despachador de notificaciones push ntfy.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from glitchhunter_ai.core.models import (
    GlitchAlert,
    GlitchSeverity,
    GlitchVerdict,
    ProductItem,
    SemanticClassificationResult,
)
from glitchhunter_ai.notifications.ntfy_client import NtfyAlertDispatcher


def create_dummy_alert(severity=GlitchSeverity.CRITICO, is_missing_zero=True):
    product = ProductItem(
        id="prod-ntfy-test",
        store_id="falabella",
        store_name="Falabella Chile",
        title="Consola PlayStation 5 Slim 1TB",
        category="consolas",
        normal_price=499990.0,
        current_price=49990.0,
        url="https://falabella.com/ps5",
    )
    verdict = GlitchVerdict(
        is_glitch=True,
        severity=severity,
        confidence_score=99.0,
        discount_percentage=90.0,
        absolute_savings_clp=450000.0,
        is_missing_zero=is_missing_zero,
        is_fake_inflation=False,
        semantic_check=SemanticClassificationResult(
            is_accessory=False,
            is_main_device=True,
            detected_item_type="consola",
            confidence=0.99,
            explanation="Consola principal verificada.",
        ),
        reason="Missing Zero Bug detectado en Falabella Chile",
    )
    return GlitchAlert(
        alert_id="ALT-TEST-NTFY-01",
        product=product,
        verdict=verdict,
        alert_url="https://falabella.com/ps5",
        created_at=datetime.now(timezone.utc),
    )


def test_ntfy_format_title_and_body():
    dispatcher = NtfyAlertDispatcher(topic="test_topic")
    alert = create_dummy_alert()

    title = dispatcher.format_title(alert)
    assert "[CRITICO]" in title
    assert "Falabella Chile" in title
    assert "-90% OFF" in title

    body = dispatcher.format_body(alert)
    assert "Consola PlayStation 5 Slim 1TB" in body
    assert "$49.990 CLP" in body
    assert "$450.000 CLP" in body
    assert "Falta de un cero" in body


def test_ntfy_send_alert_mocked():
    dispatcher = NtfyAlertDispatcher(topic="test_topic")
    alert = create_dummy_alert()

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)

        success = dispatcher.send_alert(alert)
        assert success is True
        mock_post.assert_called_once()

        args, kwargs = mock_post.call_args
        assert args[0] == "https://ntfy.sh/test_topic"
        assert kwargs["headers"]["Click"] == alert.alert_url
        assert "Comprar Ahora" in kwargs["headers"]["Actions"]
        assert kwargs["headers"]["Priority"] == "urgent"


def test_ntfy_disabled_behavior():
    dispatcher = NtfyAlertDispatcher(enabled=False)
    alert = create_dummy_alert()
    success = dispatcher.send_alert(alert)
    assert success is False
