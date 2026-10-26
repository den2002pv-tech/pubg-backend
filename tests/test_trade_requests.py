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
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
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
    assert button["web_app"]["url"] == "https://pubg-card.enzonic.dev"


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


def test_list_my_trade_requests_qualifies_ambiguous_id_column(monkeypatch):
    captured = {}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

        def fetchall(self):
            return [{"id": 17}]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(trades, "_auth", lambda _init_data: (12345, {}))
    monkeypatch.setattr(trades, "get_connection", FakeConnection)
    monkeypatch.setattr(
        trades,
        "get_trade_request",
        lambda request_id, viewer_id: {"id": request_id, "viewer_id": viewer_id},
    )

    result = asyncio.run(trades.list_my_trade_requests(scope="all", init_data="test-init-data"))

    assert result == {"requests": [{"id": 17, "viewer_id": 12345}], "count": 1}
    assert "SELECT tr.id FROM trade_requests tr" in captured["query"]
    assert captured["params"] == (12345, 12345, "all", "all", 12345, "all", 12345)


def _exchange_code_request():
    return {
        "id": 29,
        "status": "agreed",
        "exchange_code_sent_at": None,
        "offered_card_name": "Карта, которую получает получатель",
        "requested_card_name": "Карта, которую отдаёт получатель",
        "sender_telegram_id": 100,
        "sender_first_name": "Автор",
        "recipient_telegram_id": 200,
    }


def test_agreed_request_shows_code_button_only_to_sender():
    request = {
        "id": 29,
        "status": "agreed",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "sender_agreed_at": True,
        "recipient_agreed_at": True,
        "sender_completed_at": None,
        "recipient_completed_at": None,
        "exchange_code_sent_at": None,
    }

    sender_actions = [
        button["callback_data"]
        for row in trades._keyboard(request, 100)["inline_keyboard"]
        for button in row
    ]
    recipient_actions = [
        button["callback_data"]
        for row in trades._keyboard(request, 200)["inline_keyboard"]
        for button in row
    ]

    assert "tr:29:code" in sender_actions
    assert "tr:29:code" not in recipient_actions


def test_exchange_code_is_sent_only_once_to_recipient(monkeypatch):
    state = _exchange_code_request()
    sent = []
    executed = []

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            executed.append((query, params))
            if query.lstrip().startswith("UPDATE trade_requests"):
                state["exchange_code_sent_at"] = "sent"

        def fetchone(self):
            return dict(state)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(trades, "get_connection", FakeConnection)
    monkeypatch.setattr(
        trades,
        "_telegram_call",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    result = trades._deliver_exchange_code(29, 100, "  ABC-123  ")

    assert result == {"status": "sent", "request_id": 29, "notification_delivered": True}
    assert len(sent) == 2
    assert sent[0][1]["chat_id"] == 200
    assert "ABC-123" in sent[0][1]["text"]
    assert "Карта, которую получает получатель" in sent[0][1]["text"]
    assert any(
        button["callback_data"] == "tr:29:complete"
        for row in sent[0][1]["reply_markup"]["inline_keyboard"]
        for button in row
    )
    assert sent[1][1]["chat_id"] == 100
    assert any(
        button["callback_data"] == "tr:29:complete"
        for row in sent[1][1]["reply_markup"]["inline_keyboard"]
        for button in row
    )
    assert all("ABC-123" not in str(params) for _query, params in executed)
    assert state["exchange_code_sent_at"] == "sent"

    with pytest.raises(HTTPException) as exc:
        trades._deliver_exchange_code(29, 100, "ANOTHER-CODE")
    assert exc.value.status_code == 409
    assert len(sent) == 2


@pytest.mark.parametrize(
    ("actor_id", "status", "code", "expected_status"),
    [
        (200, "agreed", "ABC-123", 403),
        (100, "pending", "ABC-123", 409),
        (100, "agreed", "   ", 400),
        (100, "agreed", "ABC\n123", 400),
        (100, "agreed", "x" * 129, 400),
    ],
)
def test_exchange_code_rejects_invalid_actor_state_or_code(
    monkeypatch, actor_id, status, code, expected_status
):
    state = _exchange_code_request()
    state["status"] = status
    sent = []

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, _params):
            pass

        def fetchone(self):
            return dict(state)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(trades, "get_connection", FakeConnection)
    monkeypatch.setattr(trades, "_telegram_call", lambda method, payload: sent.append(payload))

    with pytest.raises(HTTPException) as exc:
        trades._deliver_exchange_code(29, actor_id, code)

    assert exc.value.status_code == expected_status
    assert sent == []


def test_exchange_code_delivery_failure_does_not_mark_code_sent(monkeypatch):
    state = _exchange_code_request()

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, _params):
            pass

        def fetchone(self):
            return dict(state)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(trades, "get_connection", FakeConnection)

    def fail_send(_method, _payload):
        raise HTTPException(status_code=502, detail="Telegram unavailable")

    monkeypatch.setattr(trades, "_telegram_call", fail_send)

    with pytest.raises(HTTPException) as exc:
        trades._deliver_exchange_code(29, 100, "ABC-123")

    assert exc.value.status_code == 502
    assert state["exchange_code_sent_at"] is None


def test_telegram_webhook_delivers_code_from_force_reply(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    delivered = []
    sent = []
    monkeypatch.setattr(
        trades,
        "_deliver_exchange_code",
        lambda request_id, sender_id, code: delivered.append((request_id, sender_id, code))
        or {"status": "sent", "request_id": request_id, "notification_delivered": True},
    )
    monkeypatch.setattr(
        trades,
        "_telegram_call",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    result = asyncio.run(
        trades.telegram_webhook(
            {
                "message": {
                    "text": "ABC-123",
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 100},
                    "reply_to_message": {
                        "text": "Введите код обмена для заявки #29 в ответ на это сообщение.\n"
                        "Сначала создайте код в PUBG Mobile."
                    },
                }
            },
            secret="test-webhook-secret",
        )
    )

    assert result == {"ok": True}
    assert delivered == [(29, 100, "ABC-123")]
    # _deliver_exchange_code sends the sender's completion-button notification;
    # this webhook must not send a duplicate plain-text confirmation.
    assert sent == []


def test_exchange_code_api_uses_authenticated_sender(monkeypatch):
    captured = []
    monkeypatch.setattr(trades, "_auth", lambda _init_data: (100, {}))
    monkeypatch.setattr(
        trades,
        "_deliver_exchange_code",
        lambda request_id, sender_id, code: captured.append((request_id, sender_id, code))
        or {"status": "sent", "request_id": request_id, "notification_delivered": True},
    )

    result = asyncio.run(
        trades.submit_exchange_code(
            29,
            trades.ExchangeCodeSubmit(code=" ABC-123 "),
            init_data="valid-init-data",
        )
    )

    assert result == {"status": "sent", "request_id": 29, "notification_delivered": True}
    assert captured == [(29, 100, " ABC-123 ")]


def test_code_callback_prompts_sender_to_reply_with_game_code(monkeypatch):
    request = {
        "id": 29,
        "status": "agreed",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "exchange_code_sent_at": None,
    }
    sent = []
    answers = []
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(trades, "get_trade_request", lambda request_id, actor_id: dict(request))
    monkeypatch.setattr(
        trades,
        "_telegram_call",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )
    monkeypatch.setattr(trades, "_answer_callback", lambda callback_id, text: answers.append((callback_id, text)))

    result = asyncio.run(
        trades.telegram_webhook(
            {
                "callback_query": {
                    "id": "callback-1",
                    "data": "tr:29:code",
                    "from": {"id": 100},
                }
            },
            secret="test-webhook-secret",
        )
    )

    assert result == {"ok": True}
    assert sent[0][1]["chat_id"] == 100
    assert "создайте код в PUBG Mobile" in sent[0][1]["text"]
    assert sent[0][1]["reply_markup"]["force_reply"] is True
    assert answers == [("callback-1", "Ожидаю код обмена")]


def test_final_completion_transfers_cards_atomically_and_clears_wishlist(monkeypatch):
    request = {
        "id": 41,
        "status": "agreed",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "offered_card_id": "card_1",
        "requested_card_id": "card_2",
        "sender_completed_at": None,
        "recipient_completed_at": "already-confirmed",
    }
    executed = []
    committed = []

    class FakeCursor:
        rowcount = 1
        last_query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            self.last_query = query
            executed.append((query, params))

        def fetchone(self):
            if "RETURNING status" in self.last_query:
                return {"status": "completed"}
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            committed.append(True)

    reads = []
    def fake_get_trade_request(request_id, viewer_id):
        reads.append((request_id, viewer_id))
        return dict(request) if len(reads) == 1 else {**request, "status": "completed"}

    monkeypatch.setattr(trades, "get_trade_request", fake_get_trade_request)
    monkeypatch.setattr(trades, "get_connection", FakeConnection)

    result = trades._transition(41, 100, "complete")

    assert result["status"] == "completed"
    assert committed == [True]
    assert sum("UPDATE user_cards" in query for query, _ in executed) == 2
    assert sum("INSERT INTO user_cards" in query for query, _ in executed) == 2
    assert sum("DELETE FROM desired_cards" in query for query, _ in executed) == 2
    assert ("card_2", 100) in [params for query, params in executed if "INSERT INTO user_cards" in query]
    assert ("card_1", 200) in [params for query, params in executed if "INSERT INTO user_cards" in query]


def test_final_completion_rolls_back_if_a_card_is_no_longer_available(monkeypatch):
    request = {
        "id": 42,
        "status": "agreed",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "offered_card_id": "card_1",
        "requested_card_id": "card_2",
        "sender_completed_at": None,
        "recipient_completed_at": "already-confirmed",
    }
    committed = []

    class FakeCursor:
        rowcount = 1
        last_query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=()):
            self.last_query = query
            if query.lstrip().startswith("UPDATE user_cards"):
                self.rowcount = 0

        def fetchone(self):
            if "RETURNING status" in self.last_query:
                return {"status": "completed"}
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            committed.append(True)

    monkeypatch.setattr(trades, "get_trade_request", lambda request_id, viewer_id: dict(request))
    monkeypatch.setattr(trades, "get_connection", FakeConnection)

    with pytest.raises(HTTPException) as exc:
        trades._transition(42, 100, "complete")

    assert exc.value.status_code == 409
    assert committed == []

def test_completion_keyboard_is_available_for_both_players_after_code_sent():
    request = {
        "id": 73,
        "status": "agreed",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "sender_completed_at": None,
        "recipient_completed_at": None,
        "exchange_code_sent_at": "2026-10-11T17:00:00Z",
    }

    sender_keyboard = trades._keyboard(request, 100)
    recipient_keyboard = trades._keyboard(request, 200)

    for keyboard in (sender_keyboard, recipient_keyboard):
        assert any(
            button["callback_data"] == "tr:73:complete"
            for row in keyboard["inline_keyboard"]
            for button in row
        )

    assert not any(
        button["callback_data"] == "tr:73:code"
        for row in sender_keyboard["inline_keyboard"]
        for button in row
    )

def test_trade_command_resends_completion_keyboard_for_existing_request(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    request = {
        "id": 3,
        "status": "agreed",
        "sender_telegram_id": 100,
        "recipient_telegram_id": 200,
        "sender_first_name": "Lord",
        "recipient_first_name": "Игрок",
        "offered_card_name": "Мелодия",
        "requested_card_name": "Неистовая страсть",
        "sender_completed_at": None,
        "recipient_completed_at": None,
        "exchange_code_sent_at": "already-sent",
    }
    sent = []
    monkeypatch.setattr(trades, "get_trade_request", lambda request_id, viewer_id: dict(request))
    monkeypatch.setattr(
        trades,
        "_telegram_call",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )

    result = asyncio.run(
        trades.telegram_webhook(
            {
                "message": {
                    "text": "/trade 3",
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 100},
                }
            },
            secret="test-webhook-secret",
        )
    )

    assert result == {"ok": True}
    assert len(sent) == 1
    assert sent[0][1]["chat_id"] == 100
    assert any(
        button["callback_data"] == "tr:3:complete"
        for row in sent[0][1]["reply_markup"]["inline_keyboard"]
        for button in row
    )
