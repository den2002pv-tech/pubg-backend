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
