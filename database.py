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


def get_user_cards(telegram_id: int) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.rarity, c.icon, uc.quantity
                FROM user_cards uc
                JOIN users u ON u.id = uc.user_id
                JOIN cards c ON c.id = uc.card_id
                WHERE u.telegram_id = %s AND uc.quantity > 0
                ORDER BY c.id
                """,
                (telegram_id,),
            )
            return [dict(row) for row in cur.fetchall()]


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
