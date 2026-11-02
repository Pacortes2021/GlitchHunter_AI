"""
Tests del receptor de votos de Telegram (botones inline 👍 / 👎).
Verifica el parseo del callback_data, el registro del voto en la BD, la respuesta
answerCallbackQuery y el avance del offset de getUpdates.
"""

from unittest.mock import MagicMock, patch

from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.notifications.telegram_feedback import (
    TelegramFeedbackPoller,
    parse_feedback_callback,
)


def test_parse_feedback_callback():
    assert parse_feedback_callback("fb:up:ALT-RIPLEY-1234") == ("up", "ALT-RIPLEY-1234")
    assert parse_feedback_callback("fb:down:ALT-X") == ("down", "ALT-X")
    assert parse_feedback_callback("fb:maybe:ALT-X") is None
    assert parse_feedback_callback("fb:up:") is None
    assert parse_feedback_callback("otra-accion") is None
    assert parse_feedback_callback("") is None


def _updates_response(updates):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": updates}
    return resp


def test_poll_once_records_votes_and_answers_callbacks(tmp_path):
    db = PriceDatabase(db_path=tmp_path / "test_tg_feedback.db")
    db.init_db()
    poller = TelegramFeedbackPoller("123:TEST", db)

    updates = [
        {"update_id": 10, "callback_query": {"id": "cb1", "data": "fb:down:ALT-PARIS-1"}},
        {"update_id": 11, "callback_query": {"id": "cb2", "data": "fb:down:ALT-PARIS-1"}},
        {"update_id": 12, "callback_query": {"id": "cb3", "data": "fb:up:ALT-RIPLEY-2"}},
    ]

    with patch("glitchhunter_ai.notifications.telegram_feedback.requests") as mock_requests:
        mock_requests.get.return_value = _updates_response(updates)
        processed = poller.poll_once()

    assert processed == 3
    # El siguiente getUpdates confirma las actualizaciones ya procesadas
    assert poller.offset == 13

    # Cada pulsación recibe respuesta para cerrar el indicador de carga en Telegram
    answered = [c.kwargs["json"]["callback_query_id"] for c in mock_requests.post.call_args_list]
    assert answered == ["cb1", "cb2", "cb3"]

    # Dos pulsaciones sobre la misma alerta cuentan como un solo voto
    stats = db.get_feedback_stats()
    assert stats["total_votes"] == 2
    assert stats["total_downvotes"] == 1
    assert stats["total_upvotes"] == 1
