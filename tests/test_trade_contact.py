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
                "you_give": {"name": "Карта А"},
                "you_get": {"name": "Карта Б"},
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
    monkeypatch.setattr(main, "urlopen", lambda request, timeout: response)
    result = asyncio.run(main.contact_trade_match(200, init_data="signed-test-data"))

    request = main.urlopen.__self__ if hasattr(main.urlopen, "__self__") else None
    assert result["status"] == "sent"
    assert "отправлено" in result["message"].lower()
    # The message is sent to the matched player's Telegram ID, not a username.
    assert response.__enter__.return_value.read.called


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
