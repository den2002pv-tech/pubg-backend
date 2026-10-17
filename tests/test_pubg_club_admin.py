"""Tests for the admin-only PUBG MOBILE Club topic viewer."""
import asyncio
import json
from io import BytesIO

import pytest
from fastapi import HTTPException

import main


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body[:limit]


def test_pubg_club_posts_returns_text_and_media_for_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    state = {
        "topicFeedList": {
            "topicDetail": {"topicName": "cardexchange"},
            "bodys": [
                {
                    "iid": "post-1",
                    "content": "Looking to trade",
                    "image_urls": ["https://example.com/card.webp"],
                    "video": {},
                    "sendDate": "2026-10-13",
                    "type": 3,
                },
                {
                    "iid": "post-2",
                    "content": "#cardexchange",
                    "video": {"img_url": "https://example.com/preview.jpg"},
                },
            ],
        }
    }
    page = (
        "<html><script>window.__INITIAL_STATE__ = "
        + json.dumps(state)
        + ";</script></html>"
    ).encode()
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse(page)

    monkeypatch.setattr(main, "urlopen", fake_urlopen)
    token = main._make_session()
    result = asyncio.run(main.admin_pubg_club_posts("Bearer " + token))

    assert result["topic"] == "cardexchange"
    assert result["count"] == 2
    assert result["posts"][0]["iid"] == "post-1"
    assert result["posts"][0]["image_urls"] == ["https://example.com/card.webp"]
    assert result["posts"][1]["video_preview"] == "https://example.com/preview.jpg"
    assert calls and calls[0][1] == 15


def test_pubg_club_posts_requires_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    called = []
    monkeypatch.setattr(main, "urlopen", lambda *args, **kwargs: called.append(True))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.admin_pubg_club_posts(None))

    assert exc.value.status_code == 401
    assert called == []


def test_pubg_club_posts_reports_unavailable_topic_as_gateway_error(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.setattr(main, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    token = main._make_session()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.admin_pubg_club_posts("Bearer " + token))

    assert exc.value.status_code == 502
