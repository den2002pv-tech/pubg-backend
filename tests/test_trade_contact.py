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


def test_contact_trade_match_creates_persistent_request_without_username(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(main, "_validate_telegram_init_data", lambda _data: {
        "id": 100,
        "user": {"id": 100, "first_name": "Тестовый", "last_name": "Отправитель"},
    })
    monkeypatch.setattr(main, "get_or_create_user", lambda *args, **kwargs: {})
    monkeypatch.setattr(main, "get_trade_matches", lambda _telegram_id: [_match()])
    captured = {}

    def create_request(sender_id, target_id, match):
        captured.update(sender_id=sender_id, target_id=target_id, match=match)
        return {"id": 42, "notification_delivered": False}

    monkeypatch.setattr(main, "create_trade_request_for_match", create_request)
    result = asyncio.run(main.contact_trade_match(200, init_data="signed-test-data"))

    assert result["status"] == "created"
    assert result["request_id"] == 42
    assert result["notification_delivered"] is False
    assert captured["sender_id"] == 100
    assert captured["target_id"] == 200
    assert captured["match"]["telegram_id"] == 200


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
