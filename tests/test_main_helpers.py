import asyncio
import json
import time

import pytest

import main


def test_lifespan_runs_startup_database(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "startup_database", lambda: calls.append("startup"))

    async def exercise_lifespan():
        assert calls == []
        async with main.lifespan(main.app):
            assert calls == ["startup"]
        assert calls == ["startup"]

    asyncio.run(exercise_lifespan())


def test_clean_hashes_keeps_valid_values_and_normalizes_strings():
    assert main.clean_hashes({
        "full_dhash": " ab12 ",
        "visual_phash": 123,
        "empty": " ",
        "missing": None,
        "undefined": "undefined",
        "null": "NULL",
    }) == {
        "full_dhash": "ab12",
        "visual_phash": "123",
    }


@pytest.mark.parametrize("value", ["", None, "undefined", " null ", "NONE"])
def test_valid_hash_rejects_empty_or_placeholder_values(value):
    assert main.valid_hash(value) is False


@pytest.mark.parametrize("value", ["0", "abcd", " 1234 "])
def test_valid_hash_accepts_real_hash_values(value):
    assert main.valid_hash(value) is True


def test_hamming_distance_counts_different_bits():
    assert main._hamming("0", "f") == 4
    assert main._hamming("00", "03") == 2


def test_hamming_returns_none_for_invalid_hashes():
    assert main._hamming("not-hex", "01") is None


def test_catalog_audit_reports_missing_and_duplicate_icons_and_possible_counter(tmp_path, monkeypatch):
    cards_dir = tmp_path / "static" / "cards"
    cards_dir.mkdir(parents=True)
    icon_path = cards_dir / "card-1.webp"
    main.Image.new("RGB", (256, 384), "white").save(icon_path, "WEBP")
    (cards_dir / "unused.webp").write_bytes(icon_path.read_bytes())
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path / "static")
    monkeypatch.setattr(main, "CARDS_DIR", cards_dir)
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "load_db", lambda: {
        "card_1": {
            "name": "Карта 1", "hashes": {"full_dhash": "abcd"},
            "icon": "/static/cards/card-1.webp",
        },
        "card_2": {
            "name": "Карта 2", "hashes": {"full_dhash": "abcd"},
            "icon": "/static/cards/card-1.webp",
        },
        "card_3": {"name": "Карта без иконки", "hashes": {"full_dhash": "abcd"}},
    })
    monkeypatch.setattr(
        main,
        "_detect_counter",
        lambda image: (image, 3, 0.91, {"reason": "ok"}),
    )

    report = main.audit_card_catalog(authorization="admin-token")

    assert report["total"] == 3
    assert report["problem_cards"] == 3
    assert report["counts"]["possible_counter"] == 2
    assert report["counts"]["duplicate_icon"] == 2
    assert report["counts"]["missing_icon"] == 1
    assert report["unreferenced_icons"] == ["unused.webp"]
    assert report["cards"][0]["detected_quantity"] == 3


def test_catalog_audit_reports_unreadable_icon_without_aborting(tmp_path, monkeypatch):
    cards_dir = tmp_path / "static" / "cards"
    cards_dir.mkdir(parents=True)
    (cards_dir / "bad.webp").write_bytes(b"not an image")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path / "static")
    monkeypatch.setattr(main, "CARDS_DIR", cards_dir)
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)
    monkeypatch.setattr(main, "load_db", lambda: {
        "card_1": {
            "name": "Карта 1", "hashes": {"full_dhash": "abcd"},
            "icon": "/static/cards/bad.webp",
        },
    })

    report = main.audit_card_catalog(authorization="admin-token")

    assert report["counts"]["unreadable_icon"] == 1
    assert report["cards"][0]["issues"][0]["code"] == "unreadable_icon"


def test_session_token_is_valid_with_configured_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)

    token = main._make_session()

    assert main._valid_session(token) is True


def test_session_token_rejects_tampering(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    token = main._make_session()
    parts = token.split(".")
    parts[-1] = "0" * 64

    assert main._valid_session(".".join(parts)) is False


def test_session_token_rejects_expired_timestamp(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    token = main._make_session()
    now = time.time()
    monkeypatch.setattr(main.time, "time", lambda: now + main.SESSION_MAX_AGE + 10)

    assert main._valid_session(token) is False


def test_session_token_rejects_future_timestamp(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    token = main._make_session()
    now = time.time()
    monkeypatch.setattr(main.time, "time", lambda: now - 120)

    assert main._valid_session(token) is False


def test_load_db_returns_empty_dict_when_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "missing.json")

    assert main.load_db() == {}


def test_load_db_returns_empty_dict_for_invalid_json(monkeypatch, tmp_path):
    db_file = tmp_path / "broken.json"
    db_file.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr(main, "DB_FILE", db_file)

    assert main.load_db() == {}


def test_save_db_and_load_db_round_trip(monkeypatch, tmp_path):
    db_file = tmp_path / "cards.json"
    monkeypatch.setattr(main, "DB_FILE", db_file)
    expected = {
        "card_1": {"name": "Карта 1", "hashes": {"full_dhash": "abcd"}},
        "card_2": {"name": "Карта 2", "rarity": 3},
    }

    main.save_db(expected)

    assert json.loads(db_file.read_text(encoding="utf-8")) == expected
    assert main.load_db() == expected


def test_match_card_returns_exact_hash_match():
    hashes = {
        "full_phash": "aaaaaaaa",
        "full_dhash": "bbbbbbbb",
        "visual_phash": "cccccccc",
        "visual_dhash": "dddddddd",
        "frame_colorhash": "eeeeeeee",
    }
    db = {
        "card_1": {
            "name": "Карта 1",
            "hashes": dict(hashes),
        }
    }

    profile = {}
    card_id, distance = main._match_card(hashes, db, profile)

    assert card_id == "card_1"
    assert distance == 0
    assert profile == {
        "matching_calls": 1,
        "catalog_candidates_before_filter": 1,
        "matching_hash_comparisons": 5,
        "matching_candidates_after_filter": 1,
    }


def test_match_card_accepts_clear_majority_with_one_hash_outlier():
    hashes = {
        "full_phash": "0000000000000000",
        "full_dhash": "0000000000000000",
        "visual_phash": "0000000000000000",
        "visual_dhash": "0000000000000000",
        "frame_colorhash": "eeeeeeee",
    }
    stored_hashes = {
        **hashes,
        "full_dhash": "0000000000000001",
        "visual_phash": "0000000000000fff",
        "visual_dhash": "0000000003ffffff",
    }

    card_id, distance = main._match_card(
        hashes,
        {"card_1": {"name": "Карта 1", "hashes": stored_hashes}},
    )

    assert card_id == "card_1"
    assert distance == 0.5


def test_match_card_rejects_weak_fallback_with_large_hash_outlier():
    hashes = {
        "full_phash": "0000000000000000",
        "full_dhash": "0000000000000000",
        "visual_phash": "0000000000000000",
        "visual_dhash": "0000000000000000",
        "frame_colorhash": "eeeeeeee",
    }
    stored_hashes = {
        **hashes,
        "full_phash": "00000000000000ff",
        "full_dhash": "00000000000001ff",
        "visual_phash": "0000000000000fff",
        "visual_dhash": "0000000003ffffff",
    }
    db = {"card_1": {"name": "Карта 1", "hashes": stored_hashes}}

    card_id, distance = main._match_card(hashes, db)
    diagnostics = main._match_card_diagnostics(hashes, db)

    assert card_id is None
    assert distance == float("inf")
    assert diagnostics[0]["accepted"] is False
    assert "hash_outlier_too_large" in diagnostics[0]["reasons"]


def test_match_card_rejects_ambiguous_near_duplicate_cards():
    hashes = {
        "full_phash": "aaaaaaaa",
        "full_dhash": "bbbbbbbb",
        "visual_phash": "cccccccc",
        "visual_dhash": "dddddddd",
        "frame_colorhash": "eeeeeeee",
    }
    # This competing card is almost as close in every hash. A vote-count-only
    # matcher could confidently attach the scan to the wrong catalogue entry.
    near_hashes = {
        "full_phash": "aaaaaaab",
        "full_dhash": "bbbbbbba",
        "visual_phash": "cccccccd",
        "visual_dhash": "dddddddc",
        "frame_colorhash": "eeeeeeef",
    }
    db = {
        "card_burek": {"name": "Бурек", "hashes": hashes},
        "card_lentil_soup": {"name": "Чечевичный суп", "hashes": near_hashes},
    }

    card_id, distance = main._match_card(hashes, db)

    assert card_id is None
    assert distance == float("inf")
    diagnostics = main._match_card_diagnostics(hashes, db)
    assert diagnostics[0]["accepted"] is False
    assert "ambiguous_runner_up" in diagnostics[0]["reasons"]


def test_match_card_does_not_match_when_hashes_are_missing():
    hashes = {
        "full_phash": "aaaaaaaa",
        "full_dhash": "bbbbbbbb",
    }
    db = {
        "card_1": {
            "name": "Карта 1",
            "hashes": {
                "full_phash": "aaaaaaaa",
                "full_dhash": "bbbbbbbb",
            },
        }
    }

    card_id, _distance = main._match_card(hashes, db)

    assert card_id is None


def test_admin_scan_enrichment_skips_duplicate_matching_pass(monkeypatch):
    hashes = {
        "full_phash": "aaaaaaaa",
        "full_dhash": "bbbbbbbb",
        "visual_phash": "cccccccc",
        "visual_dhash": "dddddddd",
        "frame_colorhash": "eeeeeeee",
    }
    calls = []
    monkeypatch.setattr(main, "_match_card_diagnostics", lambda _hashes, _db: [])
    monkeypatch.setattr(
        main,
        "_match_card",
        lambda _hashes, _db: (calls.append(_hashes) or "card_1", 1.0),
    )

    result = main._enrich_admin_scan(
        [{
            "hashes": dict(hashes),
            "raw_hashes": dict(hashes),
            "width": 40,
            "height": 70,
        }],
        {"card_1": {"name": "Card"}},
    )

    assert len(calls) == 1
    assert result[0]["id"] == "card_1"



def test_normalize_collection_updates_is_idempotent_and_deduplicates_card_ids():
    updates = [
        {"id": " card_1 ", "quantity": 2},
        {"id": "card_2", "quantity": 1},
        {"id": "card_1", "quantity": 4},
        {"id": "card_1", "quantity": 3},
        {"name": "missing ID", "quantity": 8},
        {"id": "   ", "quantity": 9},
    ]

    first = main._normalize_collection_updates(updates)
    second = main._normalize_collection_updates(updates)

    assert first == [("card_1", 4), ("card_2", 1)]
    assert second == first


def test_normalize_collection_updates_clamps_quantity_and_rejects_invalid_values():
    assert main._normalize_collection_updates([
        {"id": "card_1", "quantity": -3},
        {"id": "card_2", "quantity": 1500},
    ]) == [("card_1", 0), ("card_2", 999)]

    with pytest.raises(main.HTTPException) as exc:
        main._normalize_collection_updates([{"id": "card_1", "quantity": "not-a-number"}])

    assert exc.value.status_code == 400


def test_confirm_collection_returns_saved_state_only_after_bulk_persistence(monkeypatch):
    calls = []
    saved_cards = [{"id": "card_1", "quantity": 4}, {"id": "card_2", "quantity": 0}]
    monkeypatch.setattr(main, "_validate_telegram_init_data", lambda _data: {"id": 12345, "user": {}})
    monkeypatch.setattr(main, "get_or_create_user", lambda _telegram_id: None)
    monkeypatch.setattr(main, "set_user_card_quantities", lambda telegram_id, updates: calls.append((telegram_id, updates)))
    monkeypatch.setattr(main, "get_user_cards", lambda _telegram_id: saved_cards)

    result = asyncio.run(main.confirm_my_collection({
        "cards": [
            {"id": "card_1", "quantity": 2},
            {"id": "card_2", "quantity": 0},
            {"id": "card_1", "quantity": 4},
        ]
    }, init_data="valid-test-init-data"))

    assert calls == [(12345, [("card_1", 4), ("card_2", 0)])]
    assert result == {"status": "ok", "saved": 2, "cards": saved_cards}


def test_confirm_collection_does_not_return_success_when_persistence_fails(monkeypatch):
    monkeypatch.setattr(main, "_validate_telegram_init_data", lambda _data: {"id": 12345, "user": {}})
    monkeypatch.setattr(main, "get_or_create_user", lambda _telegram_id: None)
    monkeypatch.setattr(
        main,
        "set_user_card_quantities",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database write failed")),
    )

    with pytest.raises(RuntimeError, match="database write failed"):
        asyncio.run(main.confirm_my_collection(
            {"cards": [{"id": "card_1", "quantity": 1}]},
            init_data="valid-test-init-data",
        ))



def test_save_card_reuses_scanned_preview_as_permanent_icon(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "card_hashes.json")
    monkeypatch.setattr(main, "TEMP_DIR", tmp_path / "temp_images")
    monkeypatch.setattr(main, "CARDS_DIR", tmp_path / "static" / "cards")
    main.TEMP_DIR.mkdir(parents=True)

    preview_path = main.TEMP_DIR / "scan-card.jpg"
    main.Image.new("RGB", (120, 180), (240, 30, 30)).save(preview_path, "JPEG")
    token = main._make_session()

    result = asyncio.run(main.save_card(
        main.CardSaveSchema(
            name="Test card",
            rarity=2,
            preview="/temp-images/scan-card.jpg",
            hashes={"full_dhash": "abcd", "full_phash": "1234"},
        ),
        authorization=f"Bearer {token}",
    ))

    assert result["card_id"] == "card_1"
    assert result["card"]["icon"] == "/static/cards/card_1.webp"
    assert (main.CARDS_DIR / "card_1.webp").is_file()
    assert main.load_db()["card_1"]["icon"] == "/static/cards/card_1.webp"


def test_admin_preview_rejects_paths_outside_temp_images(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TEMP_DIR", tmp_path / "temp_images")
    main.TEMP_DIR.mkdir(parents=True)

    with pytest.raises(main.HTTPException) as exc:
        main._load_admin_preview("/static/cards/card_1.webp")

    assert exc.value.status_code == 400


def test_github_commit_always_targets_working_branch(monkeypatch):
    monkeypatch.delenv("GITHUB_BRANCH", raising=False)
    assert main._github_commit_branch() == "feature/card-counter"

    # A stale Render setting must not redirect admin data commits to main.
    monkeypatch.setenv("GITHUB_BRANCH", "main")
    assert main._github_commit_branch() == "feature/card-counter"



def test_manual_counter_runner_reports_pass_and_mismatch(monkeypatch):
    from io import BytesIO
    import hashlib
    from PIL import Image

    image_buffer = BytesIO()
    Image.new("RGB", (32, 48), "white").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    manifest = {
        "version": 1,
        "cases": [
            {
                "id": "fixture-pass",
                "card_name": "Fixture passes",
                "image_path": "tests/fixtures/counter/images/manual-run-pass.png",
                "image_sha256": image_sha,
                "expected_quantity": 1,
            },
            {
                "id": "fixture-mismatch",
                "card_name": "Fixture mismatch",
                "image_path": "tests/fixtures/counter/images/manual-run-mismatch.png",
                "image_sha256": image_sha,
                "expected_quantity": 2,
            },
        ],
    }
    monkeypatch.setattr(main, "_counter_test_branch", lambda: "feature/card-counter")
    monkeypatch.setattr(main, "_github_read_text", lambda path, branch: json.dumps(manifest))
    monkeypatch.setattr(main, "_github_read_bytes", lambda path, branch: image_bytes)
    monkeypatch.setattr(main, "_detect_counter", lambda image: (None, 1, 0.9, {"method": "test"}))
    monkeypatch.setattr(main, "require_admin", lambda authorization: None)

    result = main.run_counter_tests(authorization="Bearer test-token")

    assert result["total"] == 2
    assert result["passed"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["status"] == "passed"
    assert result["results"][1]["status"] == "failed"
    assert result["results"][1]["expected_quantity"] == 2
    assert result["results"][1]["detected_quantity"] == 1
    assert result["results"][1]["error"] == "Ожидалось 2, распознано 1"


def test_manual_counter_runner_requires_admin(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        main,
        "require_admin",
        lambda authorization: (_ for _ in ()).throw(HTTPException(status_code=401, detail="Unauthorized")),
    )

    with pytest.raises(HTTPException) as exc:
        main.run_counter_tests(authorization=None)

    assert exc.value.status_code == 401


def test_scan_screenshot_keeps_gray_rarity_cards(monkeypatch):
    from PIL import Image
    import scanner

    image = Image.new("RGB", (120, 180), (120, 120, 120))
    monkeypatch.setattr(
        scanner,
        "detect_inventory_cards",
        lambda image: [{"row": 0, "col": 0, "box": [0, 0, 120, 180]}],
    )
    monkeypatch.setattr(
        scanner,
        "_detect_counter",
        lambda crop: (crop, 5, 0.95, {"reason": "test"}),
    )
    monkeypatch.setattr(
        scanner,
        "calculate_card_hashes",
        lambda crop, _timings=None: {
            "full_dhash": "abcd",
            "full_phash": "abcd",
            "visual_phash": "abcd",
            "visual_dhash": "abcd",
            "frame_colorhash": "abcd",
        },
    )
    monkeypatch.setattr(scanner, "encode_preview", lambda crop: "preview")
    # Even if the old low-saturation heuristic would label this frame gray/locked,
    # scan_screenshot must not silently discard a geometrically detected card.
    monkeypatch.setattr(scanner, "is_gray_locked_card", lambda crop: True)
    monkeypatch.setattr(scanner, "is_card_darkened", lambda crop: False)

    result = scanner.scan_screenshot(image)

    assert result["count"] == 1
    assert result["cards"][0]["quantity"] == 5
    assert result["cards"][0]["hashes"]["full_dhash"] == "abcd"


def test_scan_upload_profile_separates_read_decode_and_conversion():
    from io import BytesIO

    from fastapi import UploadFile
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (32, 24), (20, 30, 40)).save(buffer, format="PNG")
    upload = UploadFile(filename="fixture.png", file=BytesIO(buffer.getvalue()))
    profile = {}

    image = asyncio.run(main._read_image(upload, profile))

    assert image.mode == "RGB"
    assert image.size == (32, 24)
    assert profile["upload_format"] == "PNG"
    assert profile["upload_source_width"] == 32
    assert profile["upload_source_height"] == 24
    assert profile["upload_size_bytes"] == len(buffer.getvalue())
    assert profile["upload_read_ms"] >= 0
    assert profile["upload_image_decode_ms"] >= 0
    assert profile["upload_rgb_convert_ms"] >= 0


@pytest.mark.parametrize(("same_hashes", "expected_calls"), [(True, 1), (False, 2)])
def test_scan_skips_matching_second_pass_only_for_equal_hashes(
    monkeypatch, same_hashes, expected_calls,
):
    from copy import deepcopy

    card_hashes = {
        "full_phash": "aaaa",
        "full_dhash": "bbbb",
        "visual_phash": "cccc",
        "visual_dhash": "dddd",
        "frame_colorhash": "eeee",
    }
    raw_hashes = deepcopy(card_hashes)
    if not same_hashes:
        raw_hashes["full_dhash"] = "ffff"
    monkeypatch.setattr(
        main,
        "_read_image",
        lambda _file, _profile: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(main, "load_db", lambda: {"card_1": {}})
    monkeypatch.setattr(
        main,
        "scan_screenshot",
        lambda _image, save_previews: {
            "cards": [{
                "hashes": card_hashes,
                "raw_hashes": raw_hashes,
                "preview": "",
                "quantity": 1,
                "duplicates": 0,
                "row": 0,
                "col": 0,
            }],
            "count": 1,
            "image_size": [100, 100],
            "profile": {},
        },
    )
    calls = []

    def match(hashes, db, profile):
        calls.append(hashes)
        profile["matching_calls"] = profile.get("matching_calls", 0) + 1
        return "card_1", 1.0

    monkeypatch.setattr(main, "_match_card", match)

    result = asyncio.run(main.scan(file=object(), init_data=""))

    assert len(calls) == expected_calls
    assert result["profile"]["matched_count"] == 1
