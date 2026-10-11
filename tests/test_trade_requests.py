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
