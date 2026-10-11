import asyncio

import pytest
from fastapi import HTTPException

import trade_requests as trades


def test_pending_request_keyboard_only_allows_recipient_to_accept():
    request = {
        "id": 17,
        "status": "pending",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "sender_agreed_at": None,
        "recipient_agreed_at": None,
        "sender_completed_at": None,
        "recipient_completed_at": None,
    }
    recipient = trades._keyboard(request, 200)["inline_keyboard"]
    sender = trades._keyboard(request, 100)["inline_keyboard"]
    recipient_actions = [item["callback_data"] for row in recipient for item in row]
    sender_actions = [item["callback_data"] for row in sender for item in row]
    assert "tr:17:accept" in recipient_actions
    assert "tr:17:decline" in recipient_actions
    assert "tr:17:accept" not in sender_actions
    assert "tr:17:decline" not in sender_actions


def test_trade_request_status_labels():
    assert trades._status_label({"status": "pending"}) == "🟡 Ожидает ответа"
    assert trades._status_label({"status": "accepted"}) == "🟢 Предложение принято"
    assert trades._status_label({"status": "agreed"}) == "🤝 Обмен подтверждён обоими игроками"
    assert trades._status_label({"status": "completed"}) == "✅ Обмен завершён"



def test_request_message_and_icon_media_keep_recipient_direction(monkeypatch):
    request = {
        "id": 17,
        "status": "pending",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "sender_first_name": "Игрок",
        "sender_last_name": "А",
        "recipient_first_name": "Игрок",
        "recipient_last_name": "Б",
        "offered_card_name": "Карта А",
        "offered_card_icon": "/static/cards/card_1.webp",
        "requested_card_name": "Карта Б",
        "requested_card_icon": "/static/cards/card_2.webp",
        "sender_agreed_at": None,
        "recipient_agreed_at": None,
        "sender_completed_at": None,
        "recipient_completed_at": None,
    }
    sent = []
    albums = []
    monkeypatch.setattr(trades, "_telegram_call", lambda method, payload: sent.append((method, payload)) or {"ok": True})
    monkeypatch.setattr(trades, "_send_media_group", lambda chat_id, media: albums.append((chat_id, media)))
    monkeypatch.setattr(trades, "_card_icon_url", lambda icon: "https://cards.invalid" + icon)

    assert trades._notify_request(request, 200) is True
    message = sent[0][1]
    assert "Вы отдаёте: «Карта Б»" in message["text"]
    assert "Вы получаете: «Карта А»" in message["text"]
    assert albums[0][0] == 200
    assert "Вы отдаёте: Карта Б" in albums[0][1][0]["caption"]
    assert "Вы получаете: Карта А" in albums[0][1][1]["caption"]
    assert message["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "tr:17:accept"



@pytest.mark.parametrize(
    ("text", "expected_fragment"),
    [
        ("/start@pubg_card_trade_bot", "Привет!"),
        ("/help", "Как пользоваться"),
        ("/app", "открыть PUBG Mobile Mini App"),
    ],
)
def test_telegram_commands_reply_with_mini_app_button(monkeypatch, text, expected_fragment):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    sent = []
    monkeypatch.setattr(
        trades,
        "_telegram_call",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    result = asyncio.run(
        trades.telegram_webhook(
            {
                "message": {
                    "text": text,
                    "chat": {"id": 12345, "type": "private"},
                    "from": {"id": 12345},
                }
            },
            secret="test-webhook-secret",
        )
    )

    assert result == {"ok": True}
    assert len(sent) == 1
    method, payload = sent[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == 12345
    assert expected_fragment in payload["text"]
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == "https://pubg-app-phi.vercel.app"


def test_telegram_webhook_ignores_unknown_messages(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    sent = []
    monkeypatch.setattr(trades, "_telegram_call", lambda method, payload: sent.append(payload))

    result = asyncio.run(
        trades.telegram_webhook(
            {"message": {"text": "hello", "chat": {"id": 12345}}},
            secret="test-webhook-secret",
        )
    )

    assert result == {"ok": True}
    assert sent == []


def test_telegram_webhook_rejects_invalid_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            trades.telegram_webhook(
                {"message": {"text": "/start", "chat": {"id": 12345}}},
                secret="wrong-secret",
            )
        )

    assert exc.value.status_code == 403



def test_exchange_code_message_identifies_the_card_being_given():
    request = {
        "id": 17,
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "sender_first_name": "Денис",
        "sender_last_name": "",
        "recipient_first_name": "Лорд",
        "recipient_last_name": "",
        "offered_card_name": "Карта отправителя",
        "requested_card_name": "Карта получателя",
    }

    message = trades._exchange_code_message(request, 100, "ABCD-1234")

    assert "Денис" in message
    assert "Карта: «Карта отправителя»" in message
    assert "Код: ABCD-1234" in message


def test_list_trade_requests_qualifies_ambiguous_id_column(monkeypatch):
    executed = []

    class Cursor:
        rowcount = 0

        def execute(self, query, params):
            executed.append((query, params))

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(trades, "_validate_init_data", lambda value: {"id": 123, "user": {}})
    monkeypatch.setattr(trades, "_get_or_create_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(trades, "get_connection", lambda: Connection())

    result = asyncio.run(trades.list_my_trade_requests(scope="all", init_data="valid-test-init-data"))

    assert result == {"requests": [], "count": 0}
    assert "SELECT tr.id FROM trade_requests tr" in executed[0][0]
