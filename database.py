from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_database_url(), row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    """Create the application tables if they do not exist yet."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ,
                    last_activity_at TIMESTAMPTZ
                );

                ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT '';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT NOT NULL DEFAULT '';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT NOT NULL DEFAULT '';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;
                CREATE INDEX IF NOT EXISTS idx_users_last_activity_at ON users (last_activity_at DESC);

                CREATE TABLE IF NOT EXISTS cards (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    rarity INTEGER NOT NULL DEFAULT 1,
                    icon TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS user_cards (
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                    PRIMARY KEY (user_id, card_id)
                );

                CREATE TABLE IF NOT EXISTS desired_cards (
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, card_id)
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id BIGSERIAL PRIMARY KEY,
                    from_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    to_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
        conn.commit()


def get_or_create_user(
    telegram_id: int,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
) -> dict:
    """Create a user and refresh their public Telegram profile when available."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name, last_seen_at, last_activity_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = COALESCE(NULLIF(EXCLUDED.username, ''), users.username),
                    first_name = COALESCE(NULLIF(EXCLUDED.first_name, ''), users.first_name),
                    last_name = COALESCE(NULLIF(EXCLUDED.last_name, ''), users.last_name),
                    last_seen_at = NOW(),
                    last_activity_at = NOW()
                RETURNING id, telegram_id, username, first_name, last_name, created_at,
                          last_seen_at, last_activity_at
                """,
                (telegram_id, username.strip().lstrip("@"), first_name.strip(), last_name.strip()),
            )
            user = cur.fetchone()
        conn.commit()
    return dict(user)


def get_admin_users(search: str = "", online_seconds: int = 90, limit: int = 50, offset: int = 0) -> dict:
    """Return a paginated, searchable user directory with activity and collection stats."""
    search = (search or "").strip()[:200]
    online_seconds = max(30, min(int(online_seconds), 600))
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    pattern = f"%{search}%"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE last_activity_at >= NOW() - (%s * INTERVAL '1 second')) AS online
                   FROM users
                   WHERE (%s = '' OR telegram_id::text ILIKE %s OR username ILIKE %s OR first_name ILIKE %s OR last_name ILIKE %s)""",
                (online_seconds, search, pattern, pattern, pattern, pattern),
            )
            summary = cur.fetchone()
            cur.execute(
                """SELECT u.id, u.telegram_id, u.username, u.first_name, u.last_name,
                          u.created_at, u.last_seen_at, u.last_activity_at,
                          (u.last_activity_at >= NOW() - (%s * INTERVAL '1 second')) AS is_online,
                          COALESCE(uc.owned_cards, 0) AS owned_cards,
                          COALESCE(uc.duplicates, 0) AS duplicates,
                          COALESCE(dc.wanted_cards, 0) AS wanted_cards
                   FROM users u
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) FILTER (WHERE quantity > 0) AS owned_cards,
                                     SUM(GREATEST(quantity - 1, 0)) AS duplicates
                              FROM user_cards GROUP BY user_id) uc ON uc.user_id = u.id
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) AS wanted_cards
                              FROM desired_cards GROUP BY user_id) dc ON dc.user_id = u.id
                   WHERE (%s = '' OR u.telegram_id::text ILIKE %s OR u.username ILIKE %s OR u.first_name ILIKE %s OR u.last_name ILIKE %s)
                   ORDER BY u.last_activity_at DESC NULLS LAST, u.created_at DESC, u.id DESC
                   LIMIT %s OFFSET %s""",
                (online_seconds, search, pattern, pattern, pattern, pattern, limit, offset),
            )
            users = [dict(row) for row in cur.fetchall()]
    return {"users": users, "total": int(summary["total"]), "online": int(summary["online"]),
            "limit": limit, "offset": offset, "online_seconds": online_seconds}


def get_admin_user(telegram_id: int) -> dict | None:
    """Return one user and aggregate collection stats without join multiplication."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.id, u.telegram_id, u.username, u.first_name, u.last_name,
                          u.created_at, u.last_seen_at, u.last_activity_at,
                          COALESCE(uc.owned_cards, 0) AS owned_cards,
                          COALESCE(uc.duplicates, 0) AS duplicates,
                          COALESCE(dc.wanted_cards, 0) AS wanted_cards
                   FROM users u
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) FILTER (WHERE quantity > 0) AS owned_cards,
                                     SUM(GREATEST(quantity - 1, 0)) AS duplicates
                              FROM user_cards GROUP BY user_id) uc ON uc.user_id = u.id
                   LEFT JOIN (SELECT user_id, COUNT(DISTINCT card_id) AS wanted_cards
                              FROM desired_cards GROUP BY user_id) dc ON dc.user_id = u.id
                   WHERE u.telegram_id = %s""",
                (telegram_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def set_user_card_quantity(telegram_id: int, card_id: str, quantity: int) -> None:
    quantity = max(0, int(quantity))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_cards (user_id, card_id, quantity)
                SELECT id, %s, %s FROM users WHERE telegram_id = %s
                ON CONFLICT (user_id, card_id) DO UPDATE SET quantity = EXCLUDED.quantity
                """,
                (card_id, quantity, telegram_id),
            )
            if quantity > 0:
                cur.execute(
                    """
                    DELETE FROM desired_cards
                    WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                      AND card_id = %s
                    """,
                    (telegram_id, card_id),
                )
        conn.commit()


def clear_user_cards(telegram_id: int) -> int:
    """Set all inventory quantities to zero without deleting known cards."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_cards
                   SET quantity = 0
                 WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                   AND quantity <> 0
                """,
                (telegram_id,),
            )
            changed = cur.rowcount
        conn.commit()
    return changed


def get_desired_cards(telegram_id: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.rarity, c.icon
                FROM desired_cards dc
                JOIN users u ON u.id = dc.user_id
                JOIN cards c ON c.id = dc.card_id
                LEFT JOIN user_cards uc ON uc.user_id = u.id AND uc.card_id = c.id
                WHERE u.telegram_id = %s
                  AND COALESCE(uc.quantity, 0) = 0
                ORDER BY
                    CASE
                        WHEN c.id ~ '^card_[0-9]+$'
                            THEN CAST(SUBSTRING(c.id FROM 6) AS INTEGER)
                        ELSE 2147483647
                    END,
                    c.id
                """,
                (telegram_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def set_desired_cards(telegram_id: int, card_ids: list[str]) -> list[dict]:
    unique_ids = list(dict.fromkeys(str(x).strip() for x in card_ids if str(x).strip()))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM desired_cards
                WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)
                """,
                (telegram_id,),
            )
            if unique_ids:
                cur.execute(
                    """
                    INSERT INTO desired_cards (user_id, card_id)
                    SELECT u.id, c.id
                    FROM users u
                    JOIN cards c ON c.id = ANY(%s)
                    LEFT JOIN user_cards uc ON uc.user_id = u.id AND uc.card_id = c.id
                    WHERE u.telegram_id = %s
                      AND COALESCE(uc.quantity, 0) = 0
                    ON CONFLICT DO NOTHING
                    """,
                    (unique_ids, telegram_id),
                )
        conn.commit()
    return get_desired_cards(telegram_id)


def get_user_cards(telegram_id: int) -> list[dict]:
    """Return every master card with the user's quantity; missing rows are 0."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.rarity, c.icon,
                       COALESCE(uc.quantity, 0) AS quantity
                FROM cards c
                LEFT JOIN user_cards uc
                  ON uc.card_id = c.id
                 AND uc.user_id = (SELECT id FROM users WHERE telegram_id = %s)
                ORDER BY
                    CASE
                        WHEN c.id ~ '^card_[0-9]+$'
                            THEN CAST(SUBSTRING(c.id FROM 6) AS INTEGER)
                        ELSE 2147483647
                    END,
                    c.id
                """,
                (telegram_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def add_zero_cards(telegram_id: int, card_ids: list[str]) -> int:
    """Persist selected known cards with quantity zero."""
    unique_ids = list(dict.fromkeys(str(card_id) for card_id in card_ids if card_id))
    if not unique_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_cards (user_id, card_id, quantity)
                SELECT u.id, c.id, 0
                FROM users u
                JOIN cards c ON c.id = ANY(%s)
                WHERE u.telegram_id = %s
                ON CONFLICT (user_id, card_id) DO NOTHING
                """,
                (unique_ids, telegram_id),
            )
            added = cur.rowcount
        conn.commit()
    return added


def sync_cards(cards: dict) -> int:
    """Synchronize PostgreSQL's master catalogue with card_hashes.json.

    A non-empty, valid catalogue is authoritative: cards removed from the JSON
    are removed from PostgreSQL too. Foreign keys cascade their obsolete
    per-user inventory and wishlist rows. An empty/invalid catalogue is treated
    as unavailable and never triggers mass deletion.
    """
    valid_cards = [
        (str(card_id), info)
        for card_id, info in cards.items()
        if isinstance(info, dict)
    ]
    if not valid_cards:
        return 0

    count = 0
    active_ids = [card_id for card_id, _info in valid_cards]
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in valid_cards:
                name = str(info.get("name") or card_id)
                try:
                    rarity = int(info.get("rarity", 1))
                except (TypeError, ValueError):
                    rarity = 1
                icon = str(info.get("icon") or "")

                cur.execute(
                    """
                    INSERT INTO cards (id, name, rarity, icon)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        rarity = EXCLUDED.rarity,
                        icon = EXCLUDED.icon
                    """,
                    (card_id, name, rarity, icon),
                )
                count += 1

            cur.execute(
                "DELETE FROM cards WHERE NOT (id = ANY(%s))",
                (active_ids,),
            )
        conn.commit()
    return count



def get_trade_matches(telegram_id: int, limit: int = 50) -> list[dict]:
    """Find users with at least one reciprocal card trade; first copy is reserved."""
    limit = max(1, min(int(limit), 100))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH me AS (SELECT id FROM users WHERE telegram_id = %s),
                my_wants AS (
                    SELECT dc.card_id FROM desired_cards dc JOIN me ON me.id = dc.user_id
                    LEFT JOIN user_cards uc ON uc.user_id = dc.user_id AND uc.card_id = dc.card_id
                    WHERE COALESCE(uc.quantity, 0) = 0
                ),
                my_offers AS (
                    SELECT uc.card_id, GREATEST(uc.quantity - 1, 0) AS quantity
                    FROM user_cards uc JOIN me ON me.id = uc.user_id WHERE uc.quantity > 1
                ),
                other_wants AS (
                    SELECT dc.user_id, dc.card_id FROM desired_cards dc JOIN me ON dc.user_id <> me.id
                    LEFT JOIN user_cards uc ON uc.user_id = dc.user_id AND uc.card_id = dc.card_id
                    WHERE COALESCE(uc.quantity, 0) = 0
                ),
                other_offers AS (
                    SELECT uc.user_id, uc.card_id, GREATEST(uc.quantity - 1, 0) AS quantity
                    FROM user_cards uc JOIN me ON uc.user_id <> me.id WHERE uc.quantity > 1
                )
                SELECT u.telegram_id, u.username, u.first_name, u.last_name,
                    mine.id AS receive_card_id, mine.name AS receive_card_name, mine.icon AS receive_card_icon,
                    theirs.id AS give_card_id, theirs.name AS give_card_name, theirs.icon AS give_card_icon,
                    LEAST(my_offer.quantity, 999) AS my_available,
                    LEAST(other_offer.quantity, 999) AS their_available
                FROM me
                JOIN my_offers my_offer ON TRUE
                JOIN other_wants ow ON ow.card_id = my_offer.card_id
                JOIN users u ON u.id = ow.user_id
                JOIN cards mine ON mine.id = my_offer.card_id
                JOIN other_offers other_offer ON other_offer.user_id = u.id
                JOIN my_wants mw ON mw.card_id = other_offer.card_id
                JOIN cards theirs ON theirs.id = other_offer.card_id
                ORDER BY u.username, u.first_name, u.telegram_id, mine.id, theirs.id
                """,
                (telegram_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    grouped: dict[int, dict] = {}
    for row in rows:
        other_id = int(row["telegram_id"])
        match = grouped.setdefault(other_id, {
            "telegram_id": other_id, "username": row.get("username") or "",
            "first_name": row.get("first_name") or "", "last_name": row.get("last_name") or "", "trades": [],
        })
        trade = {
            "you_give": {"id": row["receive_card_id"], "name": row["receive_card_name"],
                         "icon": row["receive_card_icon"] or "", "available": int(row["my_available"])},
            "you_get": {"id": row["give_card_id"], "name": row["give_card_name"],
                        "icon": row["give_card_icon"] or "", "available": int(row["their_available"])},
        }
        if not any(x["you_give"]["id"] == trade["you_give"]["id"] and x["you_get"]["id"] == trade["you_get"]["id"] for x in match["trades"]):
            match["trades"].append(trade)
    result = list(grouped.values())
    result.sort(key=lambda x: (-len(x["trades"]), (x["username"] or "").lower(), x["telegram_id"]))
    return result[:limit]
