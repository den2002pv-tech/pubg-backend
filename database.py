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
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

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


def get_or_create_user(telegram_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id)
                VALUES (%s)
                ON CONFLICT (telegram_id) DO UPDATE SET telegram_id = EXCLUDED.telegram_id
                RETURNING id, telegram_id, created_at
                """,
                (telegram_id,),
            )
            user = cur.fetchone()
        conn.commit()
    return dict(user)


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
        conn.commit()


def clear_user_cards(telegram_id: int) -> int:
    """Remove all inventory rows belonging to one Telegram user."""
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
            deleted = cur.rowcount
        conn.commit()
    return deleted


def get_desired_cards(telegram_id: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.rarity, c.icon
                FROM desired_cards dc
                JOIN users u ON u.id = dc.user_id
                JOIN cards c ON c.id = dc.card_id
                WHERE u.telegram_id = %s
                ORDER BY
                    CASE WHEN c.id ~ '^card_[0-9]+
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
                        WHEN c.id ~ '^card_[0-9]+def sync_cards(cards: dict) -> int:
    """Copy card metadata from card_hashes.json into PostgreSQL."""
    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in cards.items():
                if not isinstance(info, dict):
                    continue

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
                    (str(card_id), name, rarity, icon),
                )
                count += 1
        conn.commit()
    return count

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
    """Copy card metadata from card_hashes.json into PostgreSQL."""
    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in cards.items():
                if not isinstance(info, dict):
                    continue

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
                    (str(card_id), name, rarity, icon),
                )
                count += 1
        conn.commit()
    return count

                         THEN CAST(SUBSTRING(c.id FROM 6) AS INTEGER)
                         ELSE 2147483647 END,
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
                "DELETE FROM desired_cards WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s)",
                (telegram_id,),
            )
            if unique_ids:
                cur.execute(
                    """
                    INSERT INTO desired_cards (user_id, card_id)
                    SELECT u.id, c.id
                    FROM users u
                    JOIN cards c ON c.id = ANY(%s)
                    WHERE u.telegram_id = %s
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
                        WHEN c.id ~ '^card_[0-9]+def sync_cards(cards: dict) -> int:
    """Copy card metadata from card_hashes.json into PostgreSQL."""
    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in cards.items():
                if not isinstance(info, dict):
                    continue

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
                    (str(card_id), name, rarity, icon),
                )
                count += 1
        conn.commit()
    return count

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
    """Copy card metadata from card_hashes.json into PostgreSQL."""
    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for card_id, info in cards.items():
                if not isinstance(info, dict):
                    continue

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
                    (str(card_id), name, rarity, icon),
                )
                count += 1
        conn.commit()
    return count
