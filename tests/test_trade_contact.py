import asyncio
import json
from unittest.mock import MagicMock

import pytest

import main


def _match():
    return {
        "telegram_id": 200,
        "username": "",
        "first_name": "Другой",
        "last_name": "Игрок",
        "trades": [
            {
                "you_give": {"name": "Карта А", "icon": "/static/cards/card_1.webp"},
                "you_get": {"name": "Карта Б", "icon": "/static/cards/card_2.webp"},
            }
        ],
    }


def test_contact_trade_match_sends_bot_message_without_username(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(main, "_validate_telegram_init_data", lambda _data: {
        "id": 100,
        "user": {"id": 100, "first_name": "Тестовый", "last_name": "Отправитель"},
    })
    monkeypatch.setattr(main, "get_or_create_user", lambda *args, **kwargs: {})
    monkeypatch.setattr(main, "get_trade_matches", lambda _telegram_id: [_match()])

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true, "result": {}}'
    captured = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        captured.append({
            "url": request.full_url,
            "payload": payload,
            "timeout": timeout,
        })
        return response

    monkeypatch.setattr(main, "urlopen", fake_urlopen)
    result = asyncio.run(main.contact_trade_match(200, init_data="signed-test-data"))

    assert result["status"] == "sent"
    media_call = next(call for call in captured if call["url"].endswith("/sendMediaGroup"))
    message_call = next(call for call in captured if call["url"].endswith("/sendMessage"))
    assert media_call["payload"]["chat_id"] == 200
    assert media_call["timeout"] == 8
    media = media_call["payload"]["media"]
    assert len(media) == 2
    assert media[0]["media"] == "https://pubg-backend-3rtc.onrender.com/static/cards/card_2.webp"
    assert "Вы отдаёте: Карта Б" in media[0]["caption"]
    assert "Вы получаете: Карта А" in media[1]["caption"]
    assert "Тестовый Отправитель" in message_call["payload"]["text"]
    assert "Карта А" in message_call["payload"]["text"]
    assert "Карта Б" in message_call["payload"]["text"]


def test_contact_trade_match_rejects_unmatched_telegram_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(main, "_validate_telegram_init_data", lambda _data: {
        "id": 100,
        "user": {"id": 100, "first_name": "Тест"},
    })
    monkeypatch.setattr(main, "get_or_create_user", lambda *args, **kwargs: {})
    monkeypatch.setattr(main, "get_trade_matches", lambda _telegram_id: [])

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.contact_trade_match(999, init_data="signed-test-data"))

    assert exc.value.status_code == 404


def test_send_telegram_message_requires_bot_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(main.HTTPException) as exc:
        main._send_telegram_message(200, "test")

    assert exc.value.status_code == 503
