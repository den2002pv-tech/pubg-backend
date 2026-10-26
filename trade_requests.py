from __future__ import annotations

import hmac
import json
import os
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app_config import get_public_base_url
from database import get_connection

router = APIRouter()
_validate_init_data: Callable[..., dict[str, Any]] | None = None
_get_or_create_user: Callable[..., dict[str, Any]] | None = None
_get_trade_matches: Callable[..., list[dict[str, Any]]] | None = None
_send_media_group: Callable[..., None] | None = None
_card_icon_url: Callable[..., str] | None = None


class TradeRequestCreate(BaseModel):
    target_telegram_id: int = Field(gt=0)
    offered_card_id: str = Field(min_length=1, max_length=100)
    requested_card_id: str = Field(min_length=1, max_length=100)


class ExchangeCodeSubmit(BaseModel):
    code: str = Field(min_length=1, max_length=128)


def register_trade_request_routes(
    app: FastAPI,
    validate_init_data: Callable[..., dict[str, Any]],
    get_or_create_user: Callable[..., dict[str, Any]],
    get_trade_matches: Callable[..., list[dict[str, Any]]],
    send_media_group: Callable[..., None],
    card_icon_url: Callable[..., str],
) -> None:
    global _validate_init_data, _get_or_create_user, _get_trade_matches
    global _send_media_group, _card_icon_url
    _validate_init_data = validate_init_data
    _get_or_create_user = get_or_create_user
    _get_trade_matches = get_trade_matches
    _send_media_group = send_media_group
    _card_icon_url = card_icon_url
    app.include_router(router)


def _auth(init_data: str) -> tuple[int, dict[str, Any]]:
    if _validate_init_data is None:
        raise HTTPException(status_code=503, detail="Trade requests are not configured")
    auth = _validate_init_data(init_data)
    user = auth.get("user") or {}
    if not auth.get("id") or not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="Не удалось подтвердить Telegram-пользователя")
    if _get_or_create_user:
        _get_or_create_user(
            int(auth["id"]),
            username=str(user.get("username") or ""),
            first_name=str(user.get("first_name") or ""),
            last_name=str(user.get("last_name") or ""),
        )
    return int(auth["id"]), user


def _create_request_record(
    sender_telegram_id: int,
    recipient_telegram_id: int,
    offered_card_id: str,
    requested_card_id: str,
) -> dict[str, Any]:
    if sender_telegram_id == recipient_telegram_id:
        raise HTTPException(status_code=400, detail="Нельзя предложить обмен самому себе")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trade_requests
                    (sender_user_id, recipient_user_id, offered_card_id, requested_card_id,
                     offered_card_name, offered_card_icon, requested_card_name, requested_card_icon, status)
                SELECT sender.id, recipient.id, offered.id, requested.id,
                       offered.name, offered.icon, requested.name, requested.icon, 'pending'
                FROM users sender
                JOIN users recipient ON recipient.telegram_id = %s
                JOIN cards offered ON offered.id = %s
                JOIN cards requested ON requested.id = %s
                WHERE sender.telegram_id = %s
                  AND EXISTS (
                    SELECT 1 FROM user_cards inventory
                    WHERE inventory.user_id = sender.id
                      AND inventory.card_id = offered.id
                      AND inventory.quantity > 1
                  )
                  AND EXISTS (
                    SELECT 1 FROM desired_cards wanted
                    LEFT JOIN user_cards inventory
                      ON inventory.user_id = recipient.id
                     AND inventory.card_id = requested.id
                    WHERE wanted.user_id = recipient.id
                      AND wanted.card_id = offered.id
                      AND COALESCE(
                          (SELECT offered_inventory.quantity
                           FROM user_cards offered_inventory
                           WHERE offered_inventory.user_id = recipient.id
                             AND offered_inventory.card_id = requested.id),
                          0
                      ) > 1
                  )
                RETURNING id, status, created_at
                """,
                (recipient_telegram_id, offered_card_id, requested_card_id, sender_telegram_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=409,
                    detail="Обмен больше не подходит: проверьте наличие дубликата и список «Ищу»",
                )
            request_id = int(row["id"])
        conn.commit()
    return get_trade_request(request_id, sender_telegram_id)


def get_trade_request(request_id: int, viewer_telegram_id: int | None = None) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tr.id, tr.status, tr.created_at, tr.updated_at,
                       tr.sender_agreed_at, tr.recipient_agreed_at,
                       tr.sender_completed_at, tr.recipient_completed_at,
                       tr.exchange_code_sent_at,
                       sender.telegram_id AS sender_telegram_id,
                       sender.first_name AS sender_first_name, sender.last_name AS sender_last_name,
                       recipient.telegram_id AS recipient_telegram_id,
                       recipient.first_name AS recipient_first_name, recipient.last_name AS recipient_last_name,
                       tr.offered_card_id, tr.offered_card_name, tr.offered_card_icon,
                       tr.requested_card_id, tr.requested_card_name, tr.requested_card_icon
                FROM trade_requests tr
                JOIN users sender ON sender.id = tr.sender_user_id
                JOIN users recipient ON recipient.id = tr.recipient_user_id
                WHERE tr.id = %s
                """,
                (request_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Заявка на обмен не найдена")
    result = dict(row)
    if viewer_telegram_id is not None and viewer_telegram_id not in (
        int(result["sender_telegram_id"]), int(result["recipient_telegram_id"])
    ):
        raise HTTPException(status_code=403, detail="Вы не участник этой заявки")
    return result


def _status_label(request: dict[str, Any]) -> str:
    status = request["status"]
    if status == "pending":
        return "🟡 Ожидает ответа"
    if status == "accepted":
        return "🟢 Предложение принято"
    if status == "agreed":
        return "🤝 Обмен подтверждён обоими игроками"
    if status == "completed":
        return "✅ Обмен завершён"
    if status == "declined":
        return "❌ Предложение отклонено"
    if status == "cancelled":
        return "❌ Обмен отменён"
    return str(status)


def _player_name(request: dict[str, Any], prefix: str) -> str:
    return " ".join(
        str(request.get(f"{prefix}_{part}") or "").strip()
        for part in ("first_name", "last_name")
        if str(request.get(f"{prefix}_{part}") or "").strip()
    ) or "Игрок PUBG Mobile"


def _request_text(request: dict[str, Any], viewer_id: int) -> str:
    sender_id = int(request["sender_telegram_id"])
    recipient_id = int(request["recipient_telegram_id"])
    if viewer_id == recipient_id:
        gives = request["requested_card_name"]
        gets = request["offered_card_name"]
        other = _player_name(request, "sender")
    else:
        gives = request["offered_card_name"]
        gets = request["requested_card_name"]
        other = _player_name(request, "recipient")
    return (
        "🔄 Предложение обмена картами PUBG Mobile\n"
        f"Игрок: {other}\n\n"
        f"📤 Вы отдаёте: «{gives}»\n"
        f"📥 Вы получаете: «{gets}»\n\n"
        f"Статус: {_status_label(request)}\n"
        f"Номер заявки: #{request['id']}"
    )


def _telegram_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN не задан на сервере")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Не удалось связаться с Telegram Bot API") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise HTTPException(status_code=502, detail="Telegram не принял запрос")
    return result


def _keyboard(request: dict[str, Any], viewer_id: int) -> dict[str, Any]:
    request_id = int(request["id"])
    status = request["status"]
    sender_id = int(request["sender_telegram_id"])
    recipient_id = int(request["recipient_telegram_id"])
    rows: list[list[dict[str, str]]] = []
    if status == "pending" and viewer_id == recipient_id:
        rows.extend([
            [{"text": "✅ Принять обмен", "callback_data": f"tr:{request_id}:accept"}],
            [{"text": "❌ Отклонить", "callback_data": f"tr:{request_id}:decline"}],
        ])
    elif status == "accepted":
        if viewer_id == sender_id and not request.get("sender_agreed_at"):
            rows.append([{"text": "🤝 Подтвердить обмен", "callback_data": f"tr:{request_id}:agree"}])
        if viewer_id == recipient_id and not request.get("recipient_agreed_at"):
            rows.append([{"text": "🤝 Подтвердить обмен", "callback_data": f"tr:{request_id}:agree"}])
    elif status == "agreed":
        if viewer_id == sender_id and not request.get("exchange_code_sent_at"):
            rows.append([{"text": "📨 Передать код обмена", "callback_data": f"tr:{request_id}:code"}])
        completed_field = "sender_completed_at" if viewer_id == sender_id else "recipient_completed_at"
        if not request.get(completed_field):
            rows.append([{"text": "✅ Подтвердить завершение", "callback_data": f"tr:{request_id}:complete"}])
    if status in {"pending", "accepted", "agreed"}:
        rows.append([{"text": "🔎 Посмотреть карты и статус", "callback_data": f"tr:{request_id}:details"}])
        rows.append([{"text": "Отменить обмен", "callback_data": f"tr:{request_id}:cancel"}])
    return {"inline_keyboard": rows} if rows else {"inline_keyboard": []}


def _notify_request(request: dict[str, Any], recipient_id: int, include_icons: bool = True) -> bool:
    text = _request_text(request, recipient_id)
    if include_icons and _send_media_group and _card_icon_url:
        give_icon = _card_icon_url(request.get("requested_card_icon"))
        get_icon = _card_icon_url(request.get("offered_card_icon"))
        media: list[dict[str, str]] = []
        if give_icon:
            media.append({"type": "photo", "media": give_icon, "caption": f"📤 Вы отдаёте: {request['requested_card_name']}"})
        if get_icon:
            media.append({"type": "photo", "media": get_icon, "caption": f"📥 Вы получаете: {request['offered_card_name']}"})
        if len(media) >= 2:
            try:
                _send_media_group(recipient_id, media)
            except HTTPException:
                pass
    try:
        _telegram_call("sendMessage", {
            "chat_id": recipient_id,
            "text": text,
            "reply_markup": _keyboard(request, recipient_id),
        })
        return True
    except HTTPException:
        return False


def init_trade_request_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_requests (
                    id BIGSERIAL PRIMARY KEY,
                    sender_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    recipient_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    offered_card_id TEXT NOT NULL,
                    requested_card_id TEXT NOT NULL,
                    offered_card_name TEXT NOT NULL,
                    offered_card_icon TEXT NOT NULL DEFAULT '',
                    requested_card_name TEXT NOT NULL,
                    requested_card_icon TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'agreed', 'completed', 'declined', 'cancelled')),
                    sender_agreed_at TIMESTAMPTZ,
                    recipient_agreed_at TIMESTAMPTZ,
                    sender_completed_at TIMESTAMPTZ,
                    recipient_completed_at TIMESTAMPTZ,
                    exchange_code_sent_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CHECK (sender_user_id <> recipient_user_id)
                );
                ALTER TABLE trade_requests ADD COLUMN IF NOT EXISTS exchange_code_sent_at TIMESTAMPTZ;
                CREATE INDEX IF NOT EXISTS idx_trade_requests_sender_created
                    ON trade_requests (sender_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_trade_requests_recipient_created
                    ON trade_requests (recipient_user_id, created_at DESC);
                """
            )
        conn.commit()


@router.post("/me/trade-requests")
async def create_trade_request(
    data: TradeRequestCreate,
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    sender_id, _ = _auth(init_data)
    if _get_trade_matches is None:
        raise HTTPException(status_code=503, detail="Trade matching is not configured")
    match = next(
        (item for item in _get_trade_matches(sender_id) if int(item["telegram_id"]) == data.target_telegram_id),
        None,
    )
    if not match:
        raise HTTPException(status_code=404, detail="Подходящий обмен с этим игроком не найден")
    valid_pair = any(
        str((trade.get("you_give") or {}).get("id")) == data.offered_card_id
        and str((trade.get("you_get") or {}).get("id")) == data.requested_card_id
        for trade in match.get("trades", [])
    )
    if not valid_pair:
        raise HTTPException(status_code=409, detail="Эта пара карт больше не подходит для обмена")
    request = _create_request_record(sender_id, data.target_telegram_id, data.offered_card_id, data.requested_card_id)
    delivered = _notify_request(request, data.target_telegram_id)
    return {"status": "created", "notification_delivered": delivered, "request": request}


@router.get("/me/trade-requests")
async def list_my_trade_requests(
    scope: str = "all",
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    viewer_id, _ = _auth(init_data)
    if scope not in {"all", "incoming", "outgoing"}:
        raise HTTPException(status_code=400, detail="scope должен быть all, incoming или outgoing")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tr.id FROM trade_requests tr
                JOIN users sender ON sender.id = tr.sender_user_id
                JOIN users recipient ON recipient.id = tr.recipient_user_id
                WHERE (sender.telegram_id = %s OR recipient.telegram_id = %s)
                  AND (
                    %s = 'all'
                    OR (%s = 'incoming' AND recipient.telegram_id = %s)
                    OR (%s = 'outgoing' AND sender.telegram_id = %s)
                  )
                ORDER BY tr.created_at DESC
                LIMIT 100
                """,
                (viewer_id, viewer_id, scope, scope, viewer_id, scope, viewer_id),
            )
            ids = [int(row["id"]) for row in cur.fetchall()]
    requests = [get_trade_request(request_id, viewer_id) for request_id in ids]
    return {"requests": requests, "count": len(requests)}


def _trade_still_available(request: dict[str, Any]) -> bool:
    """Re-check both duplicate inventories and the recipient's wishlist before agreeing."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM users sender
                    JOIN users recipient ON recipient.telegram_id = %s
                    JOIN user_cards sender_cards
                      ON sender_cards.user_id = sender.id
                     AND sender_cards.card_id = %s
                     AND sender_cards.quantity > 1
                    JOIN user_cards recipient_cards
                      ON recipient_cards.user_id = recipient.id
                     AND recipient_cards.card_id = %s
                     AND recipient_cards.quantity > 1
                    JOIN desired_cards wanted
                      ON wanted.user_id = recipient.id
                     AND wanted.card_id = %s
                    WHERE sender.telegram_id = %s
                ) AS available
                """,
                (
                    int(request["recipient_telegram_id"]),
                    str(request["offered_card_id"]),
                    str(request["requested_card_id"]),
                    str(request["offered_card_id"]),
                    int(request["sender_telegram_id"]),
                ),
            )
            row = cur.fetchone()
    return bool(row and row["available"])


def _transition(request_id: int, actor_id: int, action: str) -> dict[str, Any]:
    request = get_trade_request(request_id, actor_id)
    sender_id = int(request["sender_telegram_id"])
    recipient_id = int(request["recipient_telegram_id"])
    if action in {"accept", "agree"} and not _trade_still_available(request):
        raise HTTPException(
            status_code=409,
            detail="Обмен больше не актуален: одна из карт или список «Ищу» изменились",
        )
    with get_connection() as conn:
        with conn.cursor() as cur:
            if action == "accept":
                if actor_id != recipient_id or request["status"] != "pending":
                    raise HTTPException(status_code=409, detail="Это предложение нельзя принять")
                cur.execute(
                    """UPDATE trade_requests SET status='accepted', recipient_agreed_at=NOW(), updated_at=NOW()
                       WHERE id=%s AND status='pending'""",
                    (request_id,),
                )
            elif action == "decline":
                if actor_id != recipient_id or request["status"] != "pending":
                    raise HTTPException(status_code=409, detail="Это предложение нельзя отклонить")
                cur.execute(
                    "UPDATE trade_requests SET status='declined', updated_at=NOW() WHERE id=%s AND status='pending'",
                    (request_id,),
                )
            elif action == "agree":
                if request["status"] != "accepted":
                    raise HTTPException(status_code=409, detail="Сначала получатель должен принять предложение")
                field = "sender_agreed_at" if actor_id == sender_id else "recipient_agreed_at"
                cur.execute(
                    f"""UPDATE trade_requests SET {field}=COALESCE({field}, NOW()),
                        status=CASE WHEN (CASE WHEN %s THEN recipient_agreed_at IS NOT NULL ELSE sender_agreed_at IS NOT NULL END)
                                    THEN 'agreed' ELSE 'accepted' END,
                        updated_at=NOW() WHERE id=%s AND status='accepted'""",
                    (actor_id == sender_id, request_id),
                )
            elif action == "complete":
                if request["status"] != "agreed":
                    raise HTTPException(status_code=409, detail="Оба игрока должны подтвердить договорённость")
                field = "sender_completed_at" if actor_id == sender_id else "recipient_completed_at"
                cur.execute(
                    f"""UPDATE trade_requests SET {field}=COALESCE({field}, NOW()),
                        status=CASE WHEN (CASE WHEN %s THEN recipient_completed_at IS NOT NULL ELSE sender_completed_at IS NOT NULL END)
                                    THEN 'completed' ELSE 'agreed' END,
                        updated_at=NOW() WHERE id=%s AND status='agreed'
                        RETURNING status""",
                    (actor_id == sender_id, request_id),
                )
                updated = cur.fetchone()
                if not updated:
                    raise HTTPException(status_code=409, detail="Статус заявки уже изменился")

                # Only the confirmation that transitions the request to completed
                # transfers cards. Keep the status update and both inventory changes
                # in the same transaction so retries cannot duplicate the transfer.
                if updated["status"] == "completed":
                    cur.execute(
                        """UPDATE user_cards
                           SET quantity = quantity - 1
                           WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                             AND card_id = %s AND quantity > 0""",
                        (sender_id, str(request["offered_card_id"])),
                    )
                    if cur.rowcount != 1:
                        raise HTTPException(
                            status_code=409,
                            detail="Обмен не завершён: у отправителя больше нет передаваемой карты",
                        )

                    cur.execute(
                        """UPDATE user_cards
                           SET quantity = quantity - 1
                           WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                             AND card_id = %s AND quantity > 0""",
                        (recipient_id, str(request["requested_card_id"])),
                    )
                    if cur.rowcount != 1:
                        raise HTTPException(
                            status_code=409,
                            detail="Обмен не завершён: у получателя больше нет передаваемой карты",
                        )

                    cur.execute(
                        """INSERT INTO user_cards (user_id, card_id, quantity)
                           SELECT id, %s, 1 FROM users WHERE telegram_id = %s
                           ON CONFLICT (user_id, card_id)
                           DO UPDATE SET quantity = user_cards.quantity + 1""",
                        (str(request["requested_card_id"]), sender_id),
                    )
                    cur.execute(
                        """INSERT INTO user_cards (user_id, card_id, quantity)
                           SELECT id, %s, 1 FROM users WHERE telegram_id = %s
                           ON CONFLICT (user_id, card_id)
                           DO UPDATE SET quantity = user_cards.quantity + 1""",
                        (str(request["offered_card_id"]), recipient_id),
                    )
                    # A received card must no longer appear in the recipient's
                    # "I am looking for" list (and likewise for the sender).
                    cur.execute(
                        """DELETE FROM desired_cards
                           WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                             AND card_id = %s""",
                        (sender_id, str(request["requested_card_id"])),
                    )
                    cur.execute(
                        """DELETE FROM desired_cards
                           WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                             AND card_id = %s""",
                        (recipient_id, str(request["offered_card_id"])),
                    )
            elif action == "cancel":
                if request["status"] not in {"pending", "accepted", "agreed"}:
                    raise HTTPException(status_code=409, detail="Эту заявку уже нельзя отменить")
                cur.execute(
                    """UPDATE trade_requests SET status='cancelled', updated_at=NOW()
                       WHERE id=%s AND status IN ('pending','accepted','agreed')""",
                    (request_id,),
                )
            else:
                raise HTTPException(status_code=400, detail="Неизвестное действие")
            if cur.rowcount != 1:
                raise HTTPException(status_code=409, detail="Статус заявки уже изменился")
        conn.commit()
    return get_trade_request(request_id, actor_id)


@router.post("/me/trade-requests/{request_id}/{action}")
async def update_my_trade_request(
    request_id: int,
    action: str,
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    viewer_id, _ = _auth(init_data)
    request = _transition(request_id, viewer_id, action)
    other_id = int(
        request["recipient_telegram_id"]
        if viewer_id == int(request["sender_telegram_id"])
        else request["sender_telegram_id"]
    )
    delivered = []
    for participant_id in (viewer_id, other_id):
        try:
            _telegram_call("sendMessage", {
                "chat_id": participant_id,
                "text": _request_text(request, participant_id),
                "reply_markup": _keyboard(request, participant_id),
            })
            delivered.append(participant_id)
        except HTTPException:
            # The database state is authoritative; a missing bot chat must not undo it.
            continue
    return {
        "status": request["status"],
        "request": request,
        "notified_participants": delivered,
    }


def _deliver_exchange_code(request_id: int, sender_telegram_id: int, code: str) -> dict[str, Any]:
    """Deliver a PUBG-generated exchange code without persisting the code itself."""
    clean_code = code.strip()
    if not clean_code or len(clean_code) > 128 or any(ord(char) < 32 for char in clean_code):
        raise HTTPException(status_code=400, detail="Введите код длиной от 1 до 128 символов без переносов строк")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tr.id, tr.status, tr.exchange_code_sent_at,
                       tr.sender_completed_at, tr.recipient_completed_at,
                       tr.offered_card_name, tr.requested_card_name,
                       sender.telegram_id AS sender_telegram_id,
                       sender.first_name AS sender_first_name,
                       recipient.telegram_id AS recipient_telegram_id
                FROM trade_requests tr
                JOIN users sender ON sender.id = tr.sender_user_id
                JOIN users recipient ON recipient.id = tr.recipient_user_id
                WHERE tr.id = %s
                FOR UPDATE OF tr
                """,
                (request_id,),
            )
            request = cur.fetchone()
            if not request:
                raise HTTPException(status_code=404, detail="Заявка на обмен не найдена")
            if int(request["sender_telegram_id"]) != sender_telegram_id:
                raise HTTPException(status_code=403, detail="Передать код может только автор предложения")
            if request["status"] != "agreed":
                raise HTTPException(status_code=409, detail="Сначала оба игрока должны согласовать обмен")
            if request.get("exchange_code_sent_at"):
                raise HTTPException(status_code=409, detail="Код по этой заявке уже отправлен")

            message = (
                f"🎴 Код обмена PUBG Mobile — заявка #{request_id}\n\n"
                f"Игрок: {str(request.get('sender_first_name') or 'Игрок')}\n"
                f"📥 Вы получаете: «{request['offered_card_name']}»\n"
                f"📤 Вы отдаёте: «{request['requested_card_name']}»\n\n"
                f"Код обмена: {clean_code}\n\n"
                "Откройте PUBG Mobile и используйте этот код. После обмена оба игрока должны подтвердить завершение в боте."
            )
            notification_request = {**dict(request), "exchange_code_sent_at": "pending"}
            try:
                _telegram_call("sendMessage", {
                    "chat_id": int(request["recipient_telegram_id"]),
                    "text": message,
                    "reply_markup": _keyboard(notification_request, int(request["recipient_telegram_id"])),
                })
            except HTTPException as exc:
                raise HTTPException(status_code=502, detail="Не удалось передать код получателю. Попробуйте ещё раз.") from exc

            cur.execute(
                """
                UPDATE trade_requests
                   SET exchange_code_sent_at = NOW(), updated_at = NOW()
                 WHERE id = %s AND status = 'agreed' AND exchange_code_sent_at IS NULL
                """,
                (request_id,),
            )
            if cur.rowcount != 1:
                raise HTTPException(status_code=409, detail="Заявка изменилась; обновите список заявок")
        conn.commit()

    # The proposer also needs the completion button when the code was sent
    # from the Mini App rather than through the Telegram bot.
    try:
        sender_request = get_trade_request(request_id, sender_telegram_id)
        _telegram_call("sendMessage", {
            "chat_id": sender_telegram_id,
            "text": f"✅ Код обмена по заявке #{request_id} передан второму игроку. После обмена подтвердите завершение кнопкой ниже.",
            "reply_markup": _keyboard(sender_request, sender_telegram_id),
        })
    except HTTPException:
        pass

    return {"status": "sent", "request_id": request_id, "notification_delivered": True}


@router.post("/me/trade-requests/{request_id}/exchange-code")
async def submit_exchange_code(
    request_id: int,
    data: ExchangeCodeSubmit,
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    sender_id, _ = _auth(init_data)
    return _deliver_exchange_code(request_id, sender_id, data.code)


def _callback_actor(callback: dict[str, Any]) -> int:
    actor = (callback.get("from") or {}).get("id")
    if not actor:
        raise HTTPException(status_code=400, detail="Missing callback actor")
    return int(actor)


def _answer_callback(callback_id: str, text: str) -> None:
    _telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": False})


@router.post("/telegram/webhook")
async def telegram_webhook(
    update: dict[str, Any],
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="TELEGRAM_WEBHOOK_SECRET не задан на сервере")
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    message = update.get("message")
    if isinstance(message, dict):
        message_text = message.get("text")
        chat = message.get("chat")
        if isinstance(message_text, str) and isinstance(chat, dict):
            command_token = message_text.strip().split(maxsplit=1)
            command = command_token[0].split("@", 1)[0].lower() if command_token else ""
            chat_id = chat.get("id")
            if command in {"/start", "/help", "/app"} and isinstance(chat_id, int) and not isinstance(chat_id, bool):
                app_keyboard = {
                    "inline_keyboard": [[{
                        "text": "🎴 Открыть PUBG Mobile",
                        "web_app": {"url": get_public_base_url()},
                    }]]
                }
                if command == "/start":
                    reply_text = (
                        "Привет! Это бот обмена карт PUBG Mobile. 🎴\n\n"
                        "Откройте приложение, чтобы управлять коллекцией и создавать заявки на обмен. "
                        "Уведомления о заявках и кнопки подтверждения будут приходить сюда."
                    )
                elif command == "/help":
                    reply_text = (
                        "Как пользоваться обменом карт PUBG Mobile:\n"
                        "1. Откройте приложение и обновите свою коллекцию.\n"
                        "2. Найдите подходящий обмен и отправьте предложение.\n"
                        "3. Ответьте на уведомление здесь с помощью кнопок.\n4. После согласования автор предложения нажимает «Передать код обмена» и отвечает на сообщение кодом из PUBG Mobile.\n\n"
                        "Не завершайте обмен в приложении игры, пока не согласовали детали друг с другом."
                    )
                else:
                    reply_text = "Нажмите кнопку ниже, чтобы открыть PUBG Mobile Mini App."
                try:
                    _telegram_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": reply_text,
                        "reply_markup": app_keyboard,
                    })
                except HTTPException:
                    # A failed reply must not cause Telegram to retry the same update indefinitely.
                    pass

            if command == "/trade" and isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat.get("type") == "private":
                arguments = message_text.strip().split()
                if len(arguments) != 2 or not arguments[1].isdigit():
                    _telegram_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Использование: /trade <номер заявки>. Например: /trade 3",
                    })
                else:
                    try:
                        trade_request = get_trade_request(int(arguments[1]), chat_id)
                        _telegram_call("sendMessage", {
                            "chat_id": chat_id,
                            "text": _request_text(trade_request, chat_id),
                            "reply_markup": _keyboard(trade_request, chat_id),
                        })
                    except HTTPException as exc:
                        _telegram_call("sendMessage", {
                            "chat_id": chat_id,
                            "text": str(exc.detail),
                        })

            reply_to = message.get("reply_to_message")
            reply_text = str((reply_to or {}).get("text") or "") if isinstance(reply_to, dict) else ""
            code_prompt = re.fullmatch(r"Введите код обмена для заявки #(\d+) в ответ на это сообщение\..*", reply_text, flags=re.DOTALL)
            sender_id = (message.get("from") or {}).get("id")
            if (
                code_prompt
                and chat.get("type") == "private"
                and isinstance(chat_id, int)
                and not isinstance(chat_id, bool)
                and isinstance(sender_id, int)
                and sender_id == chat_id
                and isinstance(message_text, str)
                and not message_text.strip().startswith("/")
            ):
                try:
                    _deliver_exchange_code(int(code_prompt.group(1)), chat_id, message_text)
                except HTTPException as exc:
                    try:
                        _telegram_call("sendMessage", {"chat_id": chat_id, "text": str(exc.detail)})
                    except HTTPException:
                        pass
        return {"ok": True}

    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return {"ok": True}
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "tr" or not parts[1].isdigit():
        _answer_callback(callback_id, "Неизвестное действие")
        return {"ok": True}
    request_id = int(parts[1])
    action = parts[2]
    actor_id = _callback_actor(callback)
    try:
        request = get_trade_request(request_id, actor_id)
        if action == "details":
            _notify_request(request, actor_id)
            _answer_callback(callback_id, "Карты и статус заявки отправлены")
            return {"ok": True}

        if action == "code":
            if actor_id != int(request["sender_telegram_id"]):
                raise HTTPException(status_code=403, detail="Передать код может только автор предложения")
            if request["status"] != "agreed":
                raise HTTPException(status_code=409, detail="Сначала оба игрока должны согласовать обмен")
            if request.get("exchange_code_sent_at"):
                raise HTTPException(status_code=409, detail="Код по этой заявке уже отправлен")
            _telegram_call("sendMessage", {
                "chat_id": actor_id,
                "text": (
                    f"Введите код обмена для заявки #{request_id} в ответ на это сообщение.\n"
                    "Сначала создайте код в PUBG Mobile. Я передам его второму игроку. "
                    "Сам код не сохраняется в нашей базе."
                ),
                "reply_markup": {"force_reply": True, "selective": True},
            })
            _answer_callback(callback_id, "Ожидаю код обмена")
            return {"ok": True}
        if action == "accept":
            if actor_id != int(request["recipient_telegram_id"]) or request["status"] != "pending":
                raise HTTPException(status_code=403, detail="Принять предложение может только получатель")
            _telegram_call("sendMessage", {
                "chat_id": actor_id,
                "text": "Вы согласны на этот обмен? Проверьте, что у вас есть нужная карта и вы готовы её обменять.",
                "reply_markup": {"inline_keyboard": [[
                    {"text": "✅ Да, принять", "callback_data": f"tr:{request_id}:accept_confirm"},
                    {"text": "↩️ Назад", "callback_data": f"tr:{request_id}:details"},
                ]]},
            })
            _answer_callback(callback_id, "Подтвердите решение")
            return {"ok": True}
        if action == "accept_confirm":
            request = _transition(request_id, actor_id, "accept")
            _answer_callback(callback_id, "Предложение принято")
        elif action in {"decline", "agree", "complete", "cancel"}:
            request = _transition(request_id, actor_id, action)
            labels = {
                "decline": "Предложение отклонено",
                "agree": "Договорённость подтверждена",
                "complete": "Подтверждение завершения сохранено",
                "cancel": "Обмен отменён",
            }
            _answer_callback(callback_id, labels[action])
        else:
            _answer_callback(callback_id, "Неизвестное действие")
            return {"ok": True}
        text = _request_text(request, actor_id)
        _telegram_call("sendMessage", {"chat_id": actor_id, "text": text, "reply_markup": _keyboard(request, actor_id)})
        other_id = int(request["recipient_telegram_id"] if actor_id == int(request["sender_telegram_id"]) else request["sender_telegram_id"])
        _telegram_call("sendMessage", {"chat_id": other_id, "text": _request_text(request, other_id), "reply_markup": _keyboard(request, other_id)})
    except HTTPException as exc:
        _answer_callback(callback_id, exc.detail[:180])
    return {"ok": True}


def create_trade_request_for_match(
    sender_telegram_id: int,
    recipient_telegram_id: int,
    match: dict[str, Any],
) -> dict[str, Any]:
    """Persist the first currently matched pair, then notify without losing the request on delivery failure."""
    trades = match.get("trades") or []
    if not trades:
        raise HTTPException(status_code=409, detail="Подходящий обмен больше не актуален")
    trade = trades[0]
    offered = trade.get("you_give") or {}
    requested = trade.get("you_get") or {}
    offered_id = str(offered.get("id") or "")
    requested_id = str(requested.get("id") or "")
    if not offered_id or not requested_id:
        raise HTTPException(status_code=409, detail="Вариант обмена не содержит идентификаторы карт")
    request = _create_request_record(sender_telegram_id, recipient_telegram_id, offered_id, requested_id)
    delivered = _notify_request(request, recipient_telegram_id)
    request["notification_delivered"] = delivered
    return request
