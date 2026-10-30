from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import math
import os
import platform
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import sentry_sdk

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from app_config import get_app_commit, get_public_base_url
from card_state_features import best_feature_splits, extract_card_visual_features
from scanner import _detect_counter, _has_counter_evidence

from pydantic import BaseModel

from scanner import calculate_card_hashes, detect_inventory_cards, diagnose_inventory_cards, scan_screenshot
from analytics import clear_scan_performance_metrics, get_analytics_events, get_analytics_summary, init_analytics_db, record_analytics_events
from database import add_zero_cards, clear_user_cards, get_admin_user, get_admin_users, get_desired_cards, get_or_create_user, get_trade_matches, get_user_cards, init_db, set_desired_cards, set_user_card_quantities, set_user_card_quantity, sync_cards
from trade_requests import create_trade_request_for_match, init_trade_request_schema, register_trade_request_routes

# Sentry is optional in local/test environments. Configure SENTRY_DSN to enable reporting.
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        # Avoid collecting request PII by default.
        send_default_pii=False,
    )

def startup_database() -> None:
    init_db()
    init_analytics_db()
    _migrate_frame_color_hashes()
    sync_cards(load_db())
    init_trade_request_schema()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    startup_database()
    yield


app = FastAPI(title="PUBG Card Scanner", lifespan=lifespan)


STATIC_DIR = Path("static")
CARDS_DIR = STATIC_DIR / "cards"
CARDS_DIR.mkdir(parents=True, exist_ok=True)

# Admin workflow previews are temporary runtime files.
TEMP_DIR = Path("temp_images")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Keep diagnostic storage bounded in memory.
# Logs contain detector diagnostics only, never credentials or source images.
SCAN_DIAGNOSTIC_LOGS: deque[dict[str, Any]] = deque(maxlen=100)
SCREENSHOT_TEST_RUN_LOCK = threading.Lock()

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/temp-images", StaticFiles(directory=str(TEMP_DIR)), name="temp-images")

DB_FILE = Path("card_hashes.json")
SESSION_MAX_AGE = 12 * 60 * 60
ADMIN_TOKEN_PREFIX = "pubg_admin_v1"

HASH_REGEN_PROGRESS: dict[str, Any] = {
    "running": False,
    "current": 0,
    "total": 0,
    "card_id": "",
    "status": "idle",
    "updated": 0,
    "error": "",
}




def load_db() -> dict[str, Any]:
    if not DB_FILE.exists():
        return {}
    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_db(data: dict[str, Any]) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_FILE)


def clean_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, val in value.items():
        if val is None:
            continue
        s = str(val).strip()
        if not s or s.lower() in {"undefined", "null", "none"}:
            continue
        result[str(key)] = s
    return result


def valid_hash(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return bool(s) and s not in {"undefined", "null", "none"}


def _session_secret() -> str:
    secret = os.getenv("ADMIN_SESSION_SECRET", "")
    if not secret:
        secret = os.getenv("ADMIN_PASSWORD", "")
    if not secret:
        # The application will reject admin requests when ADMIN_PASSWORD
        # is missing, so this value is only a defensive fallback.
        return "invalid-admin-secret"
    return secret


def _make_session() -> str:
    """
    Create a signed bearer token.

    Format:
        pubg_admin_v1.<timestamp>.<random_nonce>.<hmac>

    The token is self-contained and does not require a session database.
    """
    ts_s = str(int(time.time()))
    nonce = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    payload = f"{ADMIN_TOKEN_PREFIX}.{ts_s}.{nonce}"
    sig = hmac.new(
        _session_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def _valid_session(value: str | None) -> bool:
    if not value:
        return False

    parts = value.split(".")
    if len(parts) != 4:
        return False

    prefix, ts_s, nonce, sig = parts
    if prefix != ADMIN_TOKEN_PREFIX or not nonce or not sig:
        return False

    try:
        ts = int(ts_s)
    except ValueError:
        return False

    now = time.time()
    if now - ts > SESSION_MAX_AGE or ts > now + 60:
        return False

    payload = f"{prefix}.{ts_s}.{nonce}"
    expected = hmac.new(
        _session_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(sig, expected)


def _validate_telegram_init_data(init_data: str) -> dict[str, Any]:
    """Validate Telegram Mini App initData and return the Telegram user."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN не задан на сервере")

    from urllib.parse import parse_qsl

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Отсутствует Telegram hash")

    auth_date = pairs.get("auth_date", "")
    try:
        if time.time() - int(auth_date) > 86400:
            raise HTTPException(status_code=401, detail="Telegram initData устарел")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Некорректный Telegram auth_date") from exc

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Недействительные данные Telegram")

    try:
        user = json.loads(pairs.get("user", "{}"))
        telegram_id = int(user["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя Telegram") from exc

    return {"id": telegram_id, "user": user}


def _authorization_token(authorization: str | None) -> str | None:
    if not authorization:
        return None

    scheme, sep, token = authorization.partition(" ")
    if not sep or scheme.lower() != "bearer":
        return None

    token = token.strip()
    return token or None


def require_admin(authorization: str | None) -> None:
    if not os.getenv("ADMIN_PASSWORD"):
        raise HTTPException(
            status_code=503,
            detail="На сервере не задан ADMIN_PASSWORD",
        )

    token = _authorization_token(authorization)
    if not _valid_session(token):
        raise HTTPException(status_code=401, detail="Требуется вход в админку")


class LoginSchema(BaseModel):
    password: str


class TelegramAuthSchema(BaseModel):
    init_data: str


class CounterTestCaseSchema(BaseModel):
    preview: str
    expected_quantity: int
    detected_quantity: int = 1
    quantity_confidence: float = 0.0
    card_id: str = ""
    card_name: str = ""
    row: int | None = None
    col: int | None = None
    box: list[int] | None = None
    image_size: list[int] | None = None
    scan_duration_ms: float | None = None
    counter_debug: dict[str, Any] = {}


class CounterTestBatchSchema(BaseModel):
    cases: list[CounterTestCaseSchema]


class ScreenshotFixtureCardSchema(BaseModel):
    box: list[int]
    expected_quantity: int = 1
    state: str = "owned"
    card_name: str = ""
    card_id: str = ""
    identity_verified: bool = False


class ScreenshotFixtureSaveSchema(BaseModel):
    upload_id: str
    id: str
    title: str = ""
    cards: list[ScreenshotFixtureCardSchema]


class ScreenshotFixtureIdentityCardSchema(BaseModel):
    card_id: str = ""
    identity_verified: bool = False


class ScreenshotFixtureIdentityUpdateSchema(BaseModel):
    cards: list[ScreenshotFixtureIdentityCardSchema]


class CardSaveSchema(BaseModel):
    id: str = ""
    name: str
    rarity: int = 1
    preview: str = ""
    hash: str | None = None
    hashes: dict[str, str] | None = None
    force_new: bool = False


class CardPreviewSchema(BaseModel):
    preview: str


def _hamming(a: str, b: str) -> int | None:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return None


ICON_MATCH_THRESHOLD = float(os.getenv("ICON_MATCH_THRESHOLD", "10"))
CARD_MATCH_STRONG_DISTANCE = int(os.getenv("CARD_MATCH_STRONG_DISTANCE", "7"))
CARD_MATCH_ACCEPTABLE_DISTANCE = int(os.getenv("CARD_MATCH_ACCEPTABLE_DISTANCE", "14"))
CARD_MATCH_MIN_STRONG_HASHES = int(os.getenv("CARD_MATCH_MIN_STRONG_HASHES", "2"))
CARD_MATCH_MIN_ACCEPTABLE_HASHES = int(os.getenv("CARD_MATCH_MIN_ACCEPTABLE_HASHES", "3"))
CARD_MATCH_COLOR_MAX_RATIO = float(os.getenv("CARD_MATCH_COLOR_MAX_RATIO", "0.35"))
# A fallback for animated cards whose hashes are consistently shifted: if the
# best candidate is clearly ahead of the runner-up, 3 acceptable hashes plus
# a compatible frame color can be enough even without 2 "strong" hashes.
CARD_MATCH_CONFIDENT_MARGIN = float(os.getenv("CARD_MATCH_CONFIDENT_MARGIN", "5.0"))


def _migrate_frame_color_hashes() -> None:
    """Add frame color hashes to older cards using their saved icons."""
    db = load_db()
    changed = False

    for card_id, info in db.items():
        if not isinstance(info, dict):
            continue

        hashes = clean_hashes(info.get("hashes"))
        if valid_hash(hashes.get("frame_colorhash")):
            continue

        icon_path = CARDS_DIR / f"{card_id}.webp"
        if not icon_path.is_file():
            continue

        try:
            with Image.open(icon_path) as icon:
                new_hashes = calculate_card_hashes(icon.convert("RGB"))
        except Exception:
            continue

        frame_hash = new_hashes.get("frame_colorhash")
        if not valid_hash(frame_hash):
            continue

        hashes["frame_colorhash"] = frame_hash
        info["hashes"] = hashes
        info["hash"] = hashes.get("full_dhash") or info.get("hash", "")
        changed = True

    if changed:
        save_db(db)


def _match_card_detailed(card_hashes: dict[str, str], db: dict[str, Any], profile: dict[str, int] | None = None) -> dict[str, Any]:
    """Detailed equivalent of _match_card; thresholds and candidate ranking are unchanged."""
    candidates: list[dict[str, Any]] = []
    rejected: list[str] = []
    comparisons = 0
    if profile is not None:
        profile["matching_calls"] = profile.get("matching_calls", 0) + 1
    keys = ("full_phash", "full_dhash", "visual_phash", "visual_dhash")
    for card_id, info in db.items():
        stored = clean_hashes(info.get("hashes"))
        per_hash: dict[str, int | None] = {}
        distances: list[int] = []
        for key in keys:
            value, other = card_hashes.get(key), stored.get(key)
            d = _hamming(value, other) if value and other else None
            per_hash[key] = d
            if d is not None:
                comparisons += 1
                distances.append(d)
        if not distances:
            legacy = info.get("hash")
            d = _hamming(card_hashes.get("full_dhash", ""), str(legacy)) if legacy else None
            if d is not None:
                comparisons += 1
                distances.append(d)
                per_hash["full_dhash"] = d
        if len(distances) < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            rejected.append("unmatched_low_strong")
            continue
        distances.sort()
        strong = sum(d <= CARD_MATCH_STRONG_DISTANCE for d in distances)
        acceptable = sum(d <= CARD_MATCH_ACCEPTABLE_DISTANCE for d in distances)
        if acceptable < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            rejected.append("unmatched_low_strong")
            continue
        if distances[-1] > CARD_MATCH_ACCEPTABLE_DISTANCE + 6 and strong < CARD_MATCH_MIN_STRONG_HASHES:
            rejected.append("unmatched_outlier_hash")
            continue
        scan_color, stored_color = card_hashes.get("frame_colorhash"), stored.get("frame_colorhash")
        color_bits = None
        if scan_color and not stored_color:
            rejected.append("unmatched_color_reject")
            continue
        if scan_color and stored_color:
            color_bits = _hamming(scan_color, stored_color)
            if color_bits is None:
                rejected.append("unmatched_color_reject")
                continue
            comparisons += 1
            color_ratio = color_bits / max(1, len(scan_color) * 4)
            if color_ratio > 0.55 and strong < 3:
                rejected.append("unmatched_color_reject")
                continue
            color_penalty = color_ratio * 8.0
        else:
            color_ratio, color_penalty = 0.0, 0.0
        candidates.append({
            "id": card_id, "score": (distances[0] + distances[1]) / 2 + color_penalty,
            "strong": strong, "acceptable": acceptable, "color_ratio": color_ratio,
            "color_penalty": color_penalty, "distances": per_hash, "color_bits": color_bits,
        })
    if profile is not None:
        profile["catalog_candidates_before_filter"] = profile.get("catalog_candidates_before_filter", 0) + len(db)
        profile["matching_hash_comparisons"] = profile.get("matching_hash_comparisons", 0) + comparisons
        profile["matching_candidates_after_filter"] = profile.get("matching_candidates_after_filter", 0) + len(candidates)
    if not candidates:
        reason = ("unmatched_no_candidates" if not db else
                  "unmatched_color_reject" if "unmatched_color_reject" in rejected else
                  "unmatched_outlier_hash" if "unmatched_outlier_hash" in rejected else
                  "unmatched_low_strong" if rejected else "unmatched_no_candidates")
        return {"id": None, "candidate_id": None, "score": None, "candidate_score": None,
                "runner_up_id": None, "runner_up_score": None, "margin": None,
                "strong_count": 0, "acceptable_count": 0, "color_ratio": None,
                "color_penalty": None, "distances": None, "runner_up_distances": None, "reason": reason}
    candidates.sort(key=lambda item: item["score"])
    best = candidates[0]
    runner = candidates[1] if len(candidates) > 1 else None
    margin = runner["score"] - best["score"] if runner else float("inf")
    accepted = False
    reason = "unmatched_low_strong"
    if best["strong"] >= CARD_MATCH_MIN_STRONG_HASHES:
        if runner is None or margin > CARD_MATCH_CONFIDENT_MARGIN:
            accepted, reason = True, "matched"
        else:
            reason = "unmatched_low_margin"
    elif (best["acceptable"] >= CARD_MATCH_MIN_ACCEPTABLE_HASHES
          and best["color_ratio"] <= CARD_MATCH_COLOR_MAX_RATIO
          and margin > CARD_MATCH_CONFIDENT_MARGIN):
        accepted, reason = True, "matched"
    elif runner is not None and margin <= CARD_MATCH_CONFIDENT_MARGIN:
        reason = "unmatched_low_margin"
    elif best["color_ratio"] > CARD_MATCH_COLOR_MAX_RATIO:
        reason = "unmatched_color_reject"
    return {
        "id": best["id"] if accepted else None, "candidate_id": best["id"],
        "score": best["score"] if accepted else None, "candidate_score": best["score"],
        "runner_up_id": runner["id"] if runner else None,
        "runner_up_score": runner["score"] if runner else None,
        "margin": margin if runner else None, "strong_count": best["strong"],
        "acceptable_count": best["acceptable"], "color_ratio": best["color_ratio"],
        "color_penalty": best["color_penalty"], "distances": best["distances"],
        "frame_colorhash_bits": best["color_bits"],
        "runner_up_distances": runner["distances"] if runner else None, "reason": reason,
    }


def _match_card(card_hashes: dict[str, str], db: dict[str, Any], profile: dict[str, int] | None = None) -> tuple[str | None, float]:
    """Keep the public/internal legacy return contract unchanged."""
    result = _match_card_detailed(card_hashes, db, profile)
    return (result["id"], float(result["score"])) if result["id"] is not None else (None, float("inf"))


def _identity_hash_distances(scan_hashes: dict[str, str], info: dict[str, Any] | None) -> dict[str, Any]:
    stored = clean_hashes((info or {}).get("hashes"))
    result = {key: (_hamming(scan_hashes.get(key, ""), stored.get(key, ""))
                    if scan_hashes.get(key) and stored.get(key) else None)
              for key in ("full_phash", "full_dhash", "visual_phash", "visual_dhash")}
    scan_color, stored_color = scan_hashes.get("frame_colorhash"), stored.get("frame_colorhash")
    bits = _hamming(scan_color, stored_color) if scan_color and stored_color else None
    result["frame_colorhash_bits"] = bits
    result["frame_colorhash_ratio"] = bits / max(1, len(scan_color) * 4) if bits is not None and scan_color else None
    return result


def _identity_percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * percentile
    lo, hi = math.floor(pos), math.ceil(pos)
    value = ordered[lo] if lo == hi else ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)
    return round(float(value), 3)


def _build_identity_stats(cases: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = ("matched_ok", "wrong_id", "unmatched_no_candidates", "unmatched_low_strong",
                 "unmatched_low_margin", "unmatched_color_reject", "unmatched_outlier_hash", "unmatched_other")
    rows = [row for case in cases for row in case.get("identity_details", [])]
    counts = {key: sum(row.get("decision") == key for row in rows) for key in decisions}
    correct = [row for row in rows if row.get("decision") == "matched_ok"]
    scores = [float(row["match_score"]) for row in correct if isinstance(row.get("match_score"), (int, float))]
    margins = [float(row["margin"]) for row in correct if isinstance(row.get("margin"), (int, float))]
    hash_keys = ("full_phash", "full_dhash", "visual_phash", "visual_dhash", "frame_colorhash_ratio")
    hash_values = {key: [] for key in hash_keys}
    for row in correct:
        distances = row.get("distances_to_matched") or {}
        for key in hash_keys:
            if isinstance(distances.get(key), (int, float)):
                hash_values[key].append(float(distances[key]))
    pairs: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        expected = str(row.get("expected_card_id") or "")
        if row.get("decision") == "wrong_id" and row.get("matched_card_id"):
            key = (expected, str(row["matched_card_id"]), "wrong_id")
        elif row.get("runner_up_id"):
            key = (expected, str(row["runner_up_id"]), "runner_up")
        else:
            continue
        pairs.setdefault(key, []).append(row)
    confusions = []
    for (expected, other, relation), items in pairs.items():
        margins_for_pair = [float(x["margin"]) for x in items if isinstance(x.get("margin"), (int, float))]
        scores_for_pair = [float(x["match_score"]) for x in items if isinstance(x.get("match_score"), (int, float))]
        confusions.append({"expected_id": expected, "other_id": other, "relation": relation, "count": len(items),
                           "avg_margin": round(sum(margins_for_pair) / len(margins_for_pair), 3) if margins_for_pair else None,
                           "avg_score": round(sum(scores_for_pair) / len(scores_for_pair), 3) if scores_for_pair else None})
    confusions.sort(key=lambda x: (-x["count"], x["expected_id"], x["other_id"], x["relation"]))
    checked = len(rows)
    return {
        "checked": checked, "correct": counts["matched_ok"], "wrong": counts["wrong_id"],
        "unmatched": checked - counts["matched_ok"] - counts["wrong_id"], "by_decision": counts,
        "score_correct": {"median": _identity_percentile(scores, .5), "p90": _identity_percentile(scores, .9),
                          "max": round(max(scores), 3) if scores else None, "count": len(scores)},
        "margin_correct": {"median": _identity_percentile(margins, .5), "p90": _identity_percentile(margins, .9),
                           "min": round(min(margins), 3) if margins else None, "count": len(margins)},
        "hash_distance_correct_median": {k: _identity_percentile(v, .5) for k, v in hash_values.items()},
        "hash_distance_correct_p90": {k: _identity_percentile(v, .9) for k, v in hash_values.items()},
        "top_confusions": confusions[:10],
    }


def _match_card_diagnostics(card_hashes: dict[str, str], db: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """Return ranked match diagnostics for admin troubleshooting.

    This does not change matching behavior. It exposes the individual hash
    distances and gate results so a bad recognition can be traced to either
    the crop/detection or the matcher without dumping full card records.
    """
    standard_keys = (
        "full_phash",
        "full_dhash",
        "visual_phash",
        "visual_dhash",
    )
    rows: list[dict[str, Any]] = []

    for card_id, info in db.items():
        stored = clean_hashes(info.get("hashes"))
        pairs: list[tuple[str, int]] = []
        for key in standard_keys:
            value = card_hashes.get(key)
            other = stored.get(key)
            if value and other:
                d = _hamming(value, other)
                if d is not None:
                    pairs.append((key, d))

        distances = sorted(d for _, d in pairs)
        if not distances:
            legacy = info.get("hash")
            value = card_hashes.get("full_dhash", "")
            d = _hamming(value, str(legacy)) if legacy else None
            if d is not None:
                distances = [d]

        strong_count = sum(d <= CARD_MATCH_STRONG_DISTANCE for d in distances)
        acceptable_count = sum(d <= CARD_MATCH_ACCEPTABLE_DISTANCE for d in distances)
        max_distance = max(distances) if distances else None

        scan_color = card_hashes.get("frame_colorhash")
        stored_color = stored.get("frame_colorhash")
        color_ratio = None
        color_status = "not_checked"
        if scan_color and not stored_color:
            color_status = "stored_hash_missing"
        elif scan_color and stored_color:
            color_distance = _hamming(scan_color, stored_color)
            if color_distance is not None:
                color_ratio = round(color_distance / max(1, len(scan_color) * 4), 3)
                color_status = "ok" if color_ratio <= 0.55 else "high"

        reasons: list[str] = []
        if len(distances) < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            reasons.append("less_than_3_hashes")
        if strong_count < CARD_MATCH_MIN_STRONG_HASHES:
            reasons.append("less_than_2_strong")
        if acceptable_count < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            reasons.append("less_than_3_acceptable")
        if (
            max_distance is not None
            and max_distance > CARD_MATCH_ACCEPTABLE_DISTANCE + 6
            and strong_count < CARD_MATCH_MIN_STRONG_HASHES
        ):
            reasons.append("hash_outlier_too_large")
        if scan_color and not stored_color:
            reasons.append("stored_color_hash_missing")
        if color_ratio is not None and color_ratio > 0.55 and strong_count < 3:
            reasons.append("color_mismatch")

        if len(distances) >= 2:
            score = (distances[0] + distances[1]) / 2
            if color_ratio is not None:
                score += color_ratio * 8.0
        else:
            score = float("inf")

        rows.append({
            "id": card_id,
            "name": info.get("name", card_id),
            "distances": {key: d for key, d in pairs},
            "sorted_distances": distances,
            "strong": strong_count,
            "acceptable": acceptable_count,
            "max": max_distance,
            "color_ratio": color_ratio,
            "color_status": color_status,
            "score": None if not math.isfinite(score) else round(score, 2),
            "accepted": not reasons,
            "reasons": reasons,
        })

    rows.sort(key=lambda item: item["score"] if item["score"] is not None else float("inf"))

    # Mirror the confidence margin in _match_card so diagnostics distinguish a
    # plausible candidate from a match that is safe to accept automatically.
    if rows:
        best = rows[0]
        runner = rows[1] if len(rows) > 1 else None
        margin = (
            runner["score"] - best["score"]
            if runner and runner["score"] is not None and best["score"] is not None
            else float("inf")
        )
        best["runner_up_id"] = runner["id"] if runner else None
        best["runner_up_score"] = runner["score"] if runner else None
        best["score_margin"] = None if not math.isfinite(margin) else round(margin, 2)

        strong_match = (
            best["strong"] >= CARD_MATCH_MIN_STRONG_HASHES
            and margin > CARD_MATCH_CONFIDENT_MARGIN
        )
        fallback_ok = (
            best["acceptable"] >= CARD_MATCH_MIN_ACCEPTABLE_HASHES
            and (best["color_ratio"] is not None and best["color_ratio"] <= CARD_MATCH_COLOR_MAX_RATIO)
            and margin > CARD_MATCH_CONFIDENT_MARGIN
        )
        best["confidence_fallback"] = fallback_ok
        if best["accepted"] and (strong_match or fallback_ok):
            best["accepted"] = True
            if fallback_ok and best["strong"] < CARD_MATCH_MIN_STRONG_HASHES:
                best["reasons"] = ["accepted_by_confident_margin"]
        elif best["accepted"]:
            best["accepted"] = False
            best["reasons"].append(
                "ambiguous_runner_up" if runner and margin <= CARD_MATCH_CONFIDENT_MARGIN
                else "insufficient_match_confidence"
            )

    return rows[:max(1, limit)]



def _find_duplicate_card_for_save(
    name: str,
    hashes: dict[str, str],
    db: dict[str, Any],
) -> dict[str, Any] | None:
    """Prevent creating a second catalogue ID for an existing card.

    A confident hash match is always treated as an existing identity. If the
    matcher deliberately rejects an ambiguous result, only block creation when
    a visually close candidate also has the same normalized name. This keeps
    the matcher conservative without allowing its ambiguity fallback to create
    duplicate records for the same card.
    """
    normalized_name = " ".join(str(name or "").casefold().split())
    if not normalized_name:
        return None

    matched_id, score = _match_card(hashes, db)
    if matched_id is not None:
        info = db.get(matched_id, {})
        return {
            "id": matched_id,
            "name": info.get("name", matched_id),
            "score": round(score, 2),
            "reason": "confident_hash_match",
        }

    threshold = float(os.getenv("CARD_MATCH_THRESHOLD", "18"))
    for candidate in _match_card_diagnostics(hashes, db, limit=max(8, len(db))):
        candidate_name = " ".join(
            str(candidate.get("name") or "").casefold().split()
        )
        distances = candidate.get("sorted_distances") or []
        score = candidate.get("score")
        if candidate_name != normalized_name:
            continue
        if len(distances) < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            continue
        acceptable_count = sum(
            distance <= CARD_MATCH_ACCEPTABLE_DISTANCE for distance in distances
        )
        if acceptable_count < CARD_MATCH_MIN_ACCEPTABLE_HASHES:
            continue
        if score is None or score > threshold:
            continue

        # Same-name candidates with close grayscale hashes can be the same
        # card even when the frame-color hash changes with crop/scale/counter.
        return {
            "id": candidate["id"],
            "name": candidate.get("name", candidate["id"]),
            "score": score,
            "reason": "ambiguous_same_name_hash_match",
        }

    return None


def _next_card_id(db: dict[str, Any]) -> str:
    numbers = []

    for card_id in db:
        if not isinstance(card_id, str):
            continue

        if not card_id.startswith("card_"):
            continue

        suffix = card_id[5:]

        if suffix.isdigit():
            numbers.append(int(suffix))

    next_number = max(numbers, default=0) + 1

    while f"card_{next_number}" in db:
        next_number += 1

    return f"card_{next_number}"


def _enrich_admin_scan(
    cards: list[dict[str, Any]],
    db: dict[str, Any],
) -> list[dict[str, Any]]:
    result = []

    for card in cards:
        item = dict(card)
        diagnostics = _match_card_diagnostics(card.get("hashes", {}), db)
        item["debug"] = {
            "detector": {
                "box": card.get("box"),
                "width": card.get("width"),
                "height": card.get("height"),
                "ratio": round(card["width"] / max(1, card["height"]), 3) if card.get("height") else None,
                "row": card.get("row"),
                "col": card.get("col"),
            },
            "counter": {
                "quantity": card.get("quantity", 1),
                "confidence": card.get("quantity_confidence", 0),
                "details": card.get("counter_debug", {}),
            },
            "top_matches": diagnostics,
        }

        best_id, best_distance = _match_card(
            card.get("hashes", {}),
            db,
        )
        raw_hashes = card.get("raw_hashes", {})
        if raw_hashes != card.get("hashes", {}):
            raw_id, raw_distance = _match_card(raw_hashes, db)
            if raw_id is not None and raw_distance < best_distance:
                best_id, best_distance = raw_id, raw_distance

        matched = (
            best_id is not None
            and best_distance <= float(
                os.getenv("CARD_MATCH_THRESHOLD", "18")
            )
        )

        if matched:
            info = db[best_id]

            item.update({
                "matched": True,
                "id": best_id,
                "name": info.get("name", best_id),
                "rarity": info.get("rarity", 1),
                "distance": round(best_distance, 2),
                "icon": info.get("icon", ""),
            })
        else:
            item.update({
                "matched": False,
                "id": None,
                "name": "",
                "rarity": None,
                "distance": (
                    round(best_distance, 2)
                    if best_id is not None
                    else None
                ),
                "icon": "",
            })

        result.append(item)

    next_number = max(
        (
            int(card_id[5:])
            for card_id in db
            if (
                isinstance(card_id, str)
                and card_id.startswith("card_")
                and card_id[5:].isdigit()
            )
        ),
        default=0,
    ) + 1

    for item in result:
        if not item["matched"]:
            while f"card_{next_number}" in db:
                next_number += 1

            item["suggested_id"] = f"card_{next_number}"
            next_number += 1

    return result


async def _read_image(
    file: UploadFile,
    profile: dict[str, Any] | None = None,
) -> Image.Image:
    try:
        read_started = time.perf_counter()
        data = await file.read()
        read_ms = (time.perf_counter() - read_started) * 1000

        decode_started = time.perf_counter()
        source_image = Image.open(BytesIO(data))
        source_format = source_image.format or "UNKNOWN"
        source_width, source_height = source_image.size
        source_image.load()
        decode_ms = (time.perf_counter() - decode_started) * 1000

        convert_started = time.perf_counter()
        image = source_image.convert("RGB")
        convert_ms = (time.perf_counter() - convert_started) * 1000
        if profile is not None:
            profile.update({
                "upload_size_bytes": len(data),
                "upload_format": source_format,
                "upload_source_width": source_width,
                "upload_source_height": source_height,
                "upload_read_ms": round(read_ms, 1),
                "upload_image_decode_ms": round(decode_ms, 1),
                "upload_rgb_convert_ms": round(convert_ms, 1),
            })
        return image
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Не удалось прочитать изображение") from exc


def _prepare_icon(image: Image.Image) -> Image.Image:
    """
    UI icon: keep the card aspect ratio and fit it into a 256x384 canvas.
    WebP is much smaller than the original JPEG/PNG.
    """
    image = image.convert("RGB")
    target_w, target_h = 256, 384
    scale = min(target_w / image.width, target_h / image.height)
    new_size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    resized = image.resize(new_size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGB", (target_w, target_h), "black")
    x = (target_w - resized.width) // 2
    y = (target_h - resized.height) // 2
    canvas.paste(resized, (x, y))
    return canvas


def _save_counter_fixture(image: Image.Image, box: Any, index: int) -> str:
    """Save the original-resolution card crop used by the detector for regression tests."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return ""
    try:
        x, y, w, h = (int(value) for value in box)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > image.width or y + h > image.height:
            return ""
        crop = image.crop((x, y, x + w, y + h)).convert("RGB")
        filename = f"counter-fixture-{int(time.time() * 1000)}-{index}.png"
        crop.save(TEMP_DIR / filename, format="PNG")
        return f"/temp-images/{filename}"
    except Exception:
        return ""

def _save_admin_preview(data_url: str, index: int) -> str:
    """Persist one temporary admin card preview for the current runtime."""
    if not data_url.startswith("data:image/") or "," not in data_url:
        return data_url
    try:
        encoded = data_url.split(",", 1)[1]
        payload = base64.b64decode(encoded)
        filename = f"scan-{int(time.time() * 1000)}-{index}.jpg"
        path = TEMP_DIR / filename
        path.write_bytes(payload)
        return f"/temp-images/{filename}"
    except Exception:
        return data_url


def _extract_single_card(image: Image.Image) -> Image.Image:
    """
    For the icon tab the user normally uploads one whole card image.
    If CV can find exactly one card rectangle, use that crop. Otherwise
    treat the uploaded image itself as the card.
    """
    try:
        detected = detect_inventory_cards(image)
        if len(detected) == 1:
            x, y, w, h = detected[0]["box"]
            return image.crop((x, y, x + w, y + h))
    except Exception:
        pass
    return image


def _load_admin_preview(preview: str) -> Image.Image:
    """Load only a scanner-produced preview or an inline image for admin icon creation."""
    if not preview:
        raise HTTPException(status_code=400, detail="Нет изображения карточки для постоянной иконки")

    try:
        if preview.startswith("data:image/") and "," in preview:
            encoded = preview.split(",", 1)[1]
            payload = base64.b64decode(encoded, validate=True)
        elif preview.startswith("/temp-images/"):
            filename = preview[len("/temp-images/"):]
            if not filename or Path(filename).name != filename:
                raise HTTPException(status_code=400, detail="Некорректный путь временного изображения")
            path = TEMP_DIR / filename
            if not path.is_file() or path.resolve().parent != TEMP_DIR.resolve():
                raise HTTPException(status_code=400, detail="Временное изображение не найдено; отсканируй скриншот ещё раз")
            payload = path.read_bytes()
        else:
            raise HTTPException(status_code=400, detail="Можно использовать только изображение из текущего сканирования")

        if not payload or len(payload) > 8 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Размер изображения для иконки должен быть меньше 8 МБ")
        return Image.open(BytesIO(payload)).convert("RGB")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Не удалось прочитать изображение карточки") from exc


def _save_permanent_icon(card_id: str, image: Image.Image) -> str:
    """Persist a card crop as the canonical icon without a second browser upload."""
    if not card_id or Path(card_id).name != card_id or card_id in {".", ".."}:
        raise HTTPException(status_code=400, detail="Некорректный ID карты для сохранения иконки")
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    path = CARDS_DIR / f"{card_id}.webp"
    try:
        _prepare_icon(image).save(path, "WEBP", quality=86, method=6)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Не удалось сохранить постоянную иконку") from exc
    return f"/static/cards/{path.name}"


@app.get("/")
async def serve_index():
    return FileResponse("index.html")


@app.get("/admin")
async def serve_admin():
    return FileResponse("admin.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/cards")
async def get_cards_endpoint(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return load_db()


@app.get("/admin/catalog-audit")
def audit_card_catalog(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    db = load_db()
    results: list[dict[str, Any]] = []
    icon_hashes: dict[str, list[str]] = {}
    referenced_paths: set[Path] = set()
    counts = {
        "invalid_record": 0,
        "missing_name": 0,
        "missing_hashes": 0,
        "missing_icon": 0,
        "invalid_icon_path": 0,
        "unreadable_icon": 0,
        "possible_counter": 0,
        "counter_check_failed": 0,
        "duplicate_icon": 0,
        "low_resolution": 0,
    }

    for card_id, raw_info in db.items():
        issues: list[dict[str, str]] = []
        info = raw_info if isinstance(raw_info, dict) else {}
        if not isinstance(raw_info, dict):
            issues.append({"code": "invalid_record", "message": "Некорректная запись карты"})
        if not str(info.get("name") or "").strip():
            issues.append({"code": "missing_name", "message": "Не указано название"})
        if not clean_hashes(info.get("hashes")):
            issues.append({"code": "missing_hashes", "message": "Нет валидных хешей изображения"})

        icon_url = str(info.get("icon") or "").strip()
        icon_path: Path | None = None
        if not icon_url:
            issues.append({"code": "missing_icon", "message": "Иконка не указана"})
        elif not icon_url.startswith("/static/cards/"):
            issues.append({"code": "invalid_icon_path", "message": "Путь иконки вне /static/cards/"})
        else:
            relative_path = Path(icon_url.removeprefix("/static/"))
            candidate = (STATIC_DIR / relative_path).resolve()
            try:
                candidate.relative_to(CARDS_DIR.resolve())
            except ValueError:
                issues.append({"code": "invalid_icon_path", "message": "Путь иконки выходит за каталог карт"})
            else:
                icon_path = candidate
                referenced_paths.add(candidate)
                if not candidate.is_file():
                    issues.append({"code": "missing_icon", "message": "Файл иконки не найден"})
                    icon_path = None

        if icon_path is not None:
            try:
                image_bytes = icon_path.read_bytes()
                with Image.open(BytesIO(image_bytes)) as opened:
                    opened.load()
                    image = opened.convert("RGB")
                    width, height = image.size
                icon_hashes.setdefault(hashlib.sha256(image_bytes).hexdigest(), []).append(str(card_id))
                if width < 128 or height < 192:
                    issues.append({
                        "code": "low_resolution",
                        "message": f"Низкое разрешение иконки: {width}×{height}",
                    })
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                issues.append({"code": "unreadable_icon", "message": f"Не удалось прочитать изображение: {exc}"})
                has_counter = False
                quantity = None
                confidence = None
            else:
                try:
                    _, quantity, confidence, counter_debug = _detect_counter(image)
                    has_counter = _has_counter_evidence(confidence, counter_debug)
                    if has_counter:
                        issues.append({
                            "code": "possible_counter",
                            "message": (
                                f"Возможный счётчик количества: x{quantity} "
                                f"(уверенность детектора {confidence:.2f})"
                            ),
                        })
                except Exception as exc:
                    has_counter = False
                    quantity = None
                    confidence = None
                    issues.append({
                        "code": "counter_check_failed",
                        "message": f"Не удалось проверить счётчик: {exc}",
                    })

        results.append({
            "card_id": str(card_id),
            "name": str(info.get("name") or ""),
            "icon": icon_url,
            "issues": issues,
            "counter_detected": bool(icon_path is not None and has_counter),
            "detected_quantity": quantity if icon_path is not None else None,
            "counter_confidence": confidence if icon_path is not None else None,
        })

    duplicate_ids = {
        card_id
        for ids in icon_hashes.values() if len(ids) > 1
        for card_id in ids
    }
    for result in results:
        if result["card_id"] in duplicate_ids:
            result["issues"].append({
                "code": "duplicate_icon",
                "message": "Файл иконки полностью совпадает с иконкой другой карты",
            })

    for result in results:
        for issue in result["issues"]:
            if issue["code"] in counts:
                counts[issue["code"]] += 1

    unreferenced_icons = sorted(
        path.name
        for path in CARDS_DIR.iterdir()
        if path.is_file() and path.resolve() not in referenced_paths
    )
    return {
        "total": len(results),
        "problem_cards": sum(bool(result["issues"]) for result in results),
        "counts": counts,
        "unreferenced_icons": unreferenced_icons,
        "cards": results,
    }


@app.get("/admin/status")
async def admin_status(authorization: str | None = Header(default=None)):
    logged_in = bool(os.getenv("ADMIN_PASSWORD")) and _valid_session(
        _authorization_token(authorization)
    )
    return {
        "logged_in": logged_in,
        "cards": len(load_db()),
        "icons": sum(1 for x in load_db().values() if x.get("icon")),
        "temporary_files": sum(1 for p in TEMP_DIR.iterdir() if p.is_file()),
    }


@app.post("/me/analytics/events")
async def submit_analytics_events(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    """Store privacy-conscious, allowlisted product analytics for an authenticated Telegram user."""
    auth = _validate_telegram_init_data(init_data)
    profile = auth.get("user", {})
    user = get_or_create_user(
        auth["id"],
        username=str(profile.get("username") or ""),
        first_name=str(profile.get("first_name") or ""),
        last_name=str(profile.get("last_name") or ""),
    )
    events = data.get("events", [])
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events должен быть массивом")
    accepted = record_analytics_events(int(user["id"]), events[:20])
    return {"status": "ok", "accepted": accepted}


@app.get("/admin/analytics")
async def admin_analytics(
    authorization: str | None = Header(default=None),
    days: int = 30,
):
    require_admin(authorization)
    return get_analytics_summary(days=days)


@app.get("/admin/analytics/export")
async def export_admin_analytics(
    authorization: str | None = Header(default=None),
    days: int = 30,
):
    """Download analytics events for the selected period without user identifiers."""
    require_admin(authorization)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        **get_analytics_events(days=days),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    filename = f"pubg-analytics-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/admin/analytics/scan-performance")
async def clear_admin_scan_performance(
    authorization: str | None = Header(default=None),
):
    """Clear scan timing fields only; retain analytics events and other properties."""
    require_admin(authorization)
    cleared = clear_scan_performance_metrics()
    return {"status": "ok", "cleared_events": cleared}


@app.get("/admin/users")
async def admin_users(authorization: str | None = Header(default=None), search: str = "", online_seconds: int = 90, limit: int = 50, offset: int = 0):
    require_admin(authorization)
    return get_admin_users(search=search, online_seconds=online_seconds, limit=limit, offset=offset)


@app.get("/admin/users/{telegram_id}")
async def admin_user_detail(telegram_id: int, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    user = get_admin_user(telegram_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


@app.post("/me/activity")
async def record_my_activity(init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    """Authenticated heartbeat for Telegram Mini App online presence."""
    auth = _validate_telegram_init_data(init_data)
    user = auth["user"]
    saved = get_or_create_user(auth["id"], username=str(user.get("username") or ""),
        first_name=str(user.get("first_name") or ""), last_name=str(user.get("last_name") or ""))
    return {"status": "ok", "telegram_id": saved["telegram_id"]}


@app.post("/admin/login")
async def admin_login(data: LoginSchema):
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD не задан на сервере")

    if not hmac.compare_digest(data.password, password):
        raise HTTPException(status_code=401, detail="Неверный пароль")

    return {
        "status": "ok",
        "token": _make_session(),
        "expires_in": SESSION_MAX_AGE,
    }


@app.post("/admin/logout")
async def admin_logout():
    # Bearer tokens are kept only by the browser. Logout removes the token
    # on the client; the signed token naturally expires after SESSION_MAX_AGE.
    return {"status": "ok"}


@app.post("/admin/scan-preview")
async def scan_preview(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    started = time.perf_counter()
    log_entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "scan_preview",
        "app_commit": get_app_commit(),
    }
    try:
        image = await _read_image(file)
        log_entry["image_size"] = [image.width, image.height]
        result = scan_screenshot(image, save_previews=True)
        for index, card in enumerate(result.get("cards", [])):
            card["counter_fixture"] = _save_counter_fixture(image, card.get("box"), index)
            if card.get("preview"):
                card["preview"] = _save_admin_preview(card["preview"], index)
        db = load_db()
        result["cards"] = _enrich_admin_scan(result.get("cards", []), db)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        result["scan_duration_ms"] = duration_ms
        log_entry.update({
            "status": "ok",
            "detected_count": result.get("count", len(result.get("cards", []))),
            "geometric_detected_count": result.get("profile", {}).get("geometric_detected_count"),
            "darkened_filtered_count": result.get("profile", {}).get("darkened_filtered_count"),
            "duration_ms": duration_ms,
            "cards": [
                {
                    "row": card.get("row"),
                    "col": card.get("col"),
                    "box": card.get("box"),
                    "matched": card.get("matched"),
                    "card_id": card.get("id"),
                    "name": card.get("name"),
                    "quantity": card.get("quantity"),
                    "quantity_confidence": card.get("quantity_confidence"),
                    "counter": card.get("debug", {}).get("counter", card.get("counter_debug", {})),
                }
                for card in result.get("cards", [])
            ],
        })
        SCAN_DIAGNOSTIC_LOGS.append(log_entry)
        return result
    except Exception as exc:
        log_entry.update({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        SCAN_DIAGNOSTIC_LOGS.append(log_entry)
        raise



@app.post("/admin/scan-diagnostics")
async def scan_diagnostics(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """Run the boundary detector in diagnostic mode and return a shareable JSON report."""
    require_admin(authorization)
    started = time.perf_counter()
    log_entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "scan_diagnostics",
        "app_commit": get_app_commit(),
    }
    try:
        image = await _read_image(file)
        report = diagnose_inventory_cards(image)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        report["diagnostic_version"] = 1
        report["timestamp"] = log_entry["timestamp"]
        report["app_commit"] = log_entry["app_commit"]
        report["duration_ms"] = duration_ms
        log_entry.update({
            "status": "ok",
            "image_size": report.get("image_size"),
            "stages": report.get("stages"),
            "duration_ms": duration_ms,
        })
        SCAN_DIAGNOSTIC_LOGS.append(log_entry)
        return report
    except Exception as exc:
        log_entry.update({
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        SCAN_DIAGNOSTIC_LOGS.append(log_entry)
        raise


@app.get("/admin/scan-logs")
async def download_scan_logs(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_commit": get_app_commit(),
        "retention": "Most recent 100 scan-preview requests in this running process; cleared on restart/redeploy.",
        "count": len(SCAN_DIAGNOSTIC_LOGS),
        "logs": list(SCAN_DIAGNOSTIC_LOGS),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"pubg-scan-logs-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



COUNTER_TEST_CASES_PATH = "tests/fixtures/counter/cases.json"
COUNTER_TEST_IMAGES_PATH = "tests/fixtures/counter/images"


def _counter_test_branch() -> str:
    return os.getenv("COUNTER_TEST_BRANCH", "feature/card-counter").strip() or "feature/card-counter"


def _github_read_text(path: str, branch: str) -> str | None:
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    if not token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN не задан на сервере")
    url = f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(branch, safe='')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pubg-card-admin",
    }
    try:
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return base64.b64decode(payload.get("content", "")).decode("utf-8")
    except HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GitHub API: {exc.code} {detail[:300]}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API недоступен: {exc}") from exc


def _github_commit_files(files: dict[str, bytes], message: str, branch: str) -> dict[str, str]:
    """Commit only the supplied files to the explicitly selected working branch."""
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    if not token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN не задан на сервере")
    api = f"https://api.github.com/repos/{repo}"
    ref = _github_request("GET", f"{api}/git/ref/heads/{quote(branch, safe='')}", token)
    parent_sha = ref["object"]["sha"]
    parent = _github_request("GET", f"{api}/git/commits/{parent_sha}", token)
    entries = []
    for path, content in files.items():
        if content is None:
            # Git Trees API requires mode and type even for deletion entries.
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        blob = _github_request("POST", f"{api}/git/blobs", token, {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = _github_request("POST", f"{api}/git/trees", token, {
        "base_tree": parent["tree"]["sha"],
        "tree": entries,
    })
    commit = _github_request("POST", f"{api}/git/commits", token, {
        "message": message,
        "tree": tree["sha"],
        "parents": [parent_sha],
    })
    _github_request("PATCH", f"{api}/git/refs/heads/{quote(branch, safe='')}", token, {
        "sha": commit["sha"],
        "force": False,
    })
    return {"sha": commit["sha"], "url": commit.get("html_url", ""), "branch": branch}


def _github_read_bytes(path: str, branch: str) -> bytes | None:
    """Read a binary fixture from the selected branch without trusting local deploy files."""
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    if not token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN не задан на сервере")
    url = f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}?ref={quote(branch, safe='')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pubg-card-admin",
    }
    try:
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        encoded = payload.get("content", "")
        if encoded:
            return base64.b64decode(encoded)
        # GitHub's Contents API may omit base64 content for files larger than 1 MB.
        # Fall back to its download URL so full-size original screenshots remain usable.
        download_url = payload.get("download_url")
        if not download_url:
            raise HTTPException(status_code=502, detail=f"GitHub не вернул содержимое файла {path}")
        download_headers = {"User-Agent": "pubg-card-admin"}
        if token:
            download_headers["Authorization"] = f"Bearer {token}"
        with urlopen(Request(download_url, headers=download_headers), timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GitHub API: {exc.code} {detail[:300]}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API недоступен: {exc}") from exc


SCREENSHOT_TEST_CASES_PATH = "tests/fixtures/screenshots/cases.json"
SCREENSHOT_TEST_IMAGES_PATH = "tests/fixtures/screenshots/images"


def _screenshot_test_branch() -> str:
    return os.getenv("COUNTER_TEST_BRANCH", "feature/card-counter").strip() or "feature/card-counter"


def _screenshot_test_manifest(branch: str) -> dict[str, Any]:
    raw = _github_read_text(SCREENSHOT_TEST_CASES_PATH, branch)
    if not raw:
        return {"version": 1, "cases": []}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Некорректный cases.json тестов скриншотов") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise HTTPException(status_code=500, detail="Некорректная структура cases.json")
    return payload


def _safe_screenshot_upload_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value or ""):
        raise HTTPException(status_code=400, detail="Некорректный ID временного скриншота")
    return value


def _read_limit(path: str, collection_errors: list[str] | None = None) -> int | None:
    try:
        value = Path(path).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        if collection_errors is not None:
            collection_errors.append(f"{Path(path).name}: {type(exc).__name__}")
        return None
    if not value or value == "max":
        return None
    try:
        limit = int(value)
    except ValueError:
        if collection_errors is not None:
            collection_errors.append(f"{Path(path).name}: invalid value")
        return None
    return limit if limit > 0 else None


def _resource_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "rss_bytes": None,
        "vms_bytes": None,
        "user_cpu_seconds": None,
        "system_cpu_seconds": None,
        "container_memory_bytes": None,
        "container_cpu_seconds": None,
        "cpu_count": os.cpu_count(),
        "available_cpu_count": None,
        "cpu_limit": None,
        "memory_limit_bytes": None,
        "metric_source": "stdlib",
        "collection_errors": [],
    }
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        snapshot["user_cpu_seconds"] = float(usage.ru_utime)
        snapshot["system_cpu_seconds"] = float(usage.ru_stime)
    except (ImportError, OSError, AttributeError) as exc:
        snapshot["collection_errors"].append(f"process_cpu: {type(exc).__name__}")

    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                snapshot["rss_bytes"] = int(line.split()[1]) * 1024
            elif line.startswith("VmSize:"):
                snapshot["vms_bytes"] = int(line.split()[1]) * 1024
        if snapshot["rss_bytes"] is None or snapshot["vms_bytes"] is None:
            snapshot["collection_errors"].append("process_memory: missing /proc/self/status fields")
    except (OSError, ValueError, IndexError) as exc:
        snapshot["collection_errors"].append(f"process_memory: {type(exc).__name__}")

    collection_errors = snapshot["collection_errors"]
    memory_limit = _read_limit("/sys/fs/cgroup/memory.max", collection_errors)
    if memory_limit is None:
        memory_limit = _read_limit(
            "/sys/fs/cgroup/memory/memory.limit_in_bytes", collection_errors
        )
    snapshot["memory_limit_bytes"] = memory_limit
    container_memory = _read_limit("/sys/fs/cgroup/memory.current", collection_errors)
    if container_memory is None:
        container_memory = _read_limit(
            "/sys/fs/cgroup/memory/memory.usage_in_bytes", collection_errors
        )
    snapshot["container_memory_bytes"] = container_memory

    try:
        cpu_max = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
        if len(cpu_max) == 2 and cpu_max[0] != "max":
            quota, period = int(cpu_max[0]), int(cpu_max[1])
            if quota > 0 and period > 0:
                snapshot["cpu_limit"] = quota / period
    except FileNotFoundError:
        try:
            quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text(encoding="ascii"))
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text(encoding="ascii"))
            if quota > 0 and period > 0:
                snapshot["cpu_limit"] = quota / period
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            snapshot["collection_errors"].append(f"container_cpu_limit: {type(exc).__name__}")
    except (OSError, ValueError) as exc:
        snapshot["collection_errors"].append(f"container_cpu_limit: {type(exc).__name__}")

    try:
        usage = Path("/sys/fs/cgroup/cpu.stat").read_text(encoding="ascii")
        for line in usage.splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec":
                snapshot["container_cpu_seconds"] = int(value) / 1_000_000
                break
    except (OSError, ValueError) as exc:
        if not isinstance(exc, FileNotFoundError):
            snapshot["collection_errors"].append(f"container_cpu: {type(exc).__name__}")
        else:
            try:
                usage_ns = int(Path("/sys/fs/cgroup/cpuacct/cpuacct.usage").read_text(encoding="ascii"))
                snapshot["container_cpu_seconds"] = usage_ns / 1_000_000_000
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as fallback_exc:
                snapshot["collection_errors"].append(
                    f"container_cpu: {type(fallback_exc).__name__}"
                )

    if snapshot["cpu_limit"] is None:
        try:
            snapshot["available_cpu_count"] = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            snapshot["available_cpu_count"] = snapshot["cpu_count"]
        if snapshot["available_cpu_count"] is not None:
            snapshot["cpu_limit"] = float(snapshot["available_cpu_count"])
    else:
        try:
            snapshot["available_cpu_count"] = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            snapshot["available_cpu_count"] = snapshot["cpu_count"]
    return snapshot


def _resource_delta(before: dict[str, Any], after: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
    def delta(key: str) -> float | None:
        first, last = before.get(key), after.get(key)
        if isinstance(first, (int, float)) and isinstance(last, (int, float)):
            return round(last - first, 6)
        return None

    cpu_delta = None
    user_delta = delta("user_cpu_seconds")
    system_delta = delta("system_cpu_seconds")
    if user_delta is not None and system_delta is not None:
        cpu_delta = user_delta + system_delta
    cpu_avg = (
        round(cpu_delta / duration_seconds * 100, 2)
        if cpu_delta is not None and duration_seconds > 0 else None
    )
    return {
        "cpu_time_before": (
            before.get("user_cpu_seconds", 0) + before.get("system_cpu_seconds", 0)
            if before.get("user_cpu_seconds") is not None
            and before.get("system_cpu_seconds") is not None else None
        ),
        "cpu_time_after": (
            after.get("user_cpu_seconds", 0) + after.get("system_cpu_seconds", 0)
            if after.get("user_cpu_seconds") is not None
            and after.get("system_cpu_seconds") is not None else None
        ),
        "cpu_percent_before": None,
        "cpu_percent_after": None,
        "user_cpu_delta": user_delta,
        "system_cpu_delta": system_delta,
        "cpu_avg_percent": cpu_avg,
        "observation_duration_seconds": round(duration_seconds, 6),
        "rss_before": before.get("rss_bytes"),
        "rss_after": after.get("rss_bytes"),
        "rss_delta": delta("rss_bytes"),
        "vms_before": before.get("vms_bytes"),
        "vms_after": after.get("vms_bytes"),
        "container_memory_before": before.get("container_memory_bytes"),
        "container_memory_after": after.get("container_memory_bytes"),
        "container_cpu_seconds_delta": delta("container_cpu_seconds"),
        "cpu_limit": after.get("cpu_limit"),
        "memory_limit": after.get("memory_limit_bytes"),
    }


class _ResourceMonitor:
    def __init__(self, interval_seconds: float = 0.1):
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.start_state: dict[str, Any] = {}
        self.end_state: dict[str, Any] = {}
        self._previous: dict[str, Any] | None = None
        self._previous_at = 0.0
        self._current_case: dict[str, Any] | None = None
        self._aggregate = self._new_aggregate()
        self.collection_errors: set[str] = set()

    @staticmethod
    def _new_aggregate() -> dict[str, Any]:
        return {
            "sample_count": 0,
            "cpu_percent_sum": 0.0,
            "cpu_percent_count": 0,
            "cpu_peak_percent": None,
            "rss_peak_bytes": None,
            "container_memory_peak_bytes": None,
        }

    def __enter__(self):
        self._started_at = time.perf_counter()
        self._start_time = datetime.now(timezone.utc)
        self.start_state = _resource_snapshot()
        self._record_sample(self.start_state, time.perf_counter())
        self.thread = threading.Thread(target=self._sample_loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        self.end_state = _resource_snapshot()
        self._record_sample(self.end_state, time.perf_counter())
        self._ended_at = time.perf_counter()
        self._end_time = datetime.now(timezone.utc)

    def _sample_loop(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            self._record_sample(_resource_snapshot(), time.perf_counter())

    def _record_sample(self, snapshot: dict[str, Any], sampled_at: float) -> None:
        errors = snapshot.get("collection_errors", [])
        with self.lock:
            self.collection_errors.update(errors)
            aggregates = [self._aggregate]
            if self._current_case is not None:
                aggregates.append(self._current_case["samples"])
            for aggregate in aggregates:
                aggregate["sample_count"] += 1
                rss = snapshot.get("rss_bytes")
                if isinstance(rss, (int, float)):
                    aggregate["rss_peak_bytes"] = max(aggregate["rss_peak_bytes"] or 0, rss)
                container_memory = snapshot.get("container_memory_bytes")
                if isinstance(container_memory, (int, float)):
                    aggregate["container_memory_peak_bytes"] = max(
                        aggregate["container_memory_peak_bytes"] or 0,
                        container_memory,
                    )
            previous = self._previous
            elapsed = sampled_at - self._previous_at
            if previous is not None and elapsed > 0:
                cpu = _resource_delta(previous, snapshot, elapsed)["cpu_avg_percent"]
                if cpu is not None:
                    for aggregate in aggregates:
                        aggregate["cpu_percent_sum"] += cpu
                        aggregate["cpu_percent_count"] += 1
                        aggregate["cpu_peak_percent"] = max(
                            aggregate["cpu_peak_percent"] or 0, cpu
                        )
            self._previous = snapshot
            self._previous_at = sampled_at

    def start_case(self) -> tuple[dict[str, Any], float, dict[str, Any]]:
        before = _resource_snapshot()
        started_at = time.perf_counter()
        case_aggregate = self._new_aggregate()
        case_aggregate["sample_count"] = 1
        if isinstance(before.get("rss_bytes"), (int, float)):
            case_aggregate["rss_peak_bytes"] = before["rss_bytes"]
        if isinstance(before.get("container_memory_bytes"), (int, float)):
            case_aggregate["container_memory_peak_bytes"] = before["container_memory_bytes"]
        with self.lock:
            self.collection_errors.update(before.get("collection_errors", []))
            self._current_case = {"samples": case_aggregate}
        return before, started_at, case_aggregate

    def finish_case(
        self,
        before: dict[str, Any],
        started_at: float,
        case_aggregate: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        after = _resource_snapshot()
        duration = time.perf_counter() - started_at
        with self.lock:
            self.collection_errors.update(after.get("collection_errors", []))
            case_aggregate["sample_count"] += 1
            if isinstance(after.get("rss_bytes"), (int, float)):
                case_aggregate["rss_peak_bytes"] = max(
                    case_aggregate["rss_peak_bytes"] or 0, after["rss_bytes"]
                )
            if isinstance(after.get("container_memory_bytes"), (int, float)):
                case_aggregate["container_memory_peak_bytes"] = max(
                    case_aggregate["container_memory_peak_bytes"] or 0,
                    after["container_memory_bytes"],
                )
            self._current_case = None
        metrics = _resource_delta(before, after, duration)
        sampled = {
            "sample_count": case_aggregate["sample_count"],
            "cpu_peak_percent": case_aggregate["cpu_peak_percent"],
            "cpu_sample_avg_percent": (
                round(case_aggregate["cpu_percent_sum"] / case_aggregate["cpu_percent_count"], 2)
                if case_aggregate["cpu_percent_count"] else None
            ),
            "sampled_peak_rss_bytes": case_aggregate["rss_peak_bytes"],
            "sampled_peak_container_memory_bytes": case_aggregate["container_memory_peak_bytes"],
        }
        cpu_metrics = {
            key: value for key, value in metrics.items()
            if key.startswith("cpu_") or key in {
                "user_cpu_delta", "system_cpu_delta", "observation_duration_seconds",
            }
        }
        cpu_metrics.update({
            "sample_count": sampled["sample_count"],
            "cpu_peak_percent": sampled["cpu_peak_percent"],
            "cpu_sample_avg_percent": sampled["cpu_sample_avg_percent"],
            "container_cpu_seconds_delta": metrics["container_cpu_seconds_delta"],
            "cpu_limit": metrics["cpu_limit"],
        })
        memory_metrics = {
            key: value for key, value in metrics.items()
            if key.startswith(("rss_", "vms_", "container_memory"))
            or key == "memory_limit"
        }
        memory_metrics.update({
            "sample_count": sampled["sample_count"],
            "sampled_peak_rss_bytes": sampled["sampled_peak_rss_bytes"],
            "sampled_peak_container_memory_bytes": sampled["sampled_peak_container_memory_bytes"],
        })
        return after, {"cpu": cpu_metrics, "memory": memory_metrics}

    def summary(self) -> dict[str, Any]:
        with self.lock:
            aggregate = dict(self._aggregate)
            errors = sorted(self.collection_errors)
        cpu_delta = _resource_delta(
            self.start_state,
            self.end_state,
            max(0.0, self._ended_at - self._started_at),
        )
        return {
            "start_state": self.start_state,
            "end_state": self.end_state,
            "start_time": self._start_time.isoformat(),
            "end_time": self._end_time.isoformat(),
            "cpu": cpu_delta,
            "duration_seconds": max(0.0, self._ended_at - self._started_at),
            "sample_count": aggregate["sample_count"],
            "cpu_peak_percent": aggregate["cpu_peak_percent"],
            "cpu_sample_avg_percent": (
                round(aggregate["cpu_percent_sum"] / aggregate["cpu_percent_count"], 2)
                if aggregate["cpu_percent_count"] else None
            ),
            "sampled_peak_rss_bytes": aggregate["rss_peak_bytes"],
            "sampled_peak_container_memory_bytes": aggregate["container_memory_peak_bytes"],
            "collection_errors": errors,
        }


def _aggregate_metric(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values if isinstance(value, (int, float)))
    if not ordered:
        return {"count": 0, "sum": 0.0, "avg": None, "median": None, "min": None, "max": None, "p90": None, "p95": None}

    def percentile(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        index = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
        return ordered[index]
    return {
        "count": len(ordered),
        "sum": round(sum(ordered), 3),
        "avg": round(sum(ordered) / len(ordered), 3),
        "median": round(ordered[len(ordered) // 2] if len(ordered) % 2 else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2, 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "p90": round(percentile(90), 3),
        "p95": round(percentile(95), 3),
    }


def _aggregate_resource_metric(values: list[float]) -> dict[str, float | int | None]:
    aggregate = _aggregate_metric(values)
    return {key: value for key, value in aggregate.items() if key != "sum" and key != "p95"}


@app.get("/admin/screenshot-tests")
def get_screenshot_tests(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    branch = _screenshot_test_branch()
    cases = _screenshot_test_manifest(branch)["cases"]
    return {"count": len(cases), "branch": branch, "cases": [
        {**item, "image_url": f"/admin/screenshot-tests/image/{quote(str(item['id']), safe='')}"}
        for item in cases if isinstance(item, dict) and item.get("id")
    ]}


@app.get("/admin/screenshot-tests/analysis")
def analyze_screenshot_test_features(authorization: str | None = Header(default=None)):
    """Compare measured visual features of manually labelled owned/unowned cards."""
    require_admin(authorization)
    branch = _screenshot_test_branch()
    cases = _screenshot_test_manifest(branch)["cases"]
    samples: list[dict[str, Any]] = []
    analyzed_cases = 0
    skipped_cases = []
    for case in cases:
        path = case.get("image_path", "")
        if not isinstance(path, str) or not path.startswith(SCREENSHOT_TEST_IMAGES_PATH + "/") or ".." in Path(path).parts:
            skipped_cases.append(str(case.get("id", "?")))
            continue
        raw = _github_read_bytes(path, branch)
        if raw is None:
            skipped_cases.append(str(case.get("id", "?")))
            continue
        try:
            with Image.open(BytesIO(raw)) as source:
                source.load()
                image = source.convert("RGB")
            for card in case.get("cards", []):
                state = card.get("state", "owned")
                box = card.get("box")
                if state not in {"owned", "unowned"} or not isinstance(box, list) or len(box) != 4:
                    continue
                x, y, w, h = (int(value) for value in box)
                if x < 0 or y < 0 or w < 2 or h < 2 or x + w > image.width or y + h > image.height:
                    continue
                features = card.get("visual_features")
                if not isinstance(features, dict) or features.get("version") != 1:
                    features = extract_card_visual_features(image.crop((x, y, x + w, y + h)))
                samples.append({
                    "state": state,
                    "features": features,
                    "case_id": str(case.get("id", "?")),
                    "card_id": str(card.get("card_id", "")),
                })
            analyzed_cases += 1
        except Exception:
            skipped_cases.append(str(case.get("id", "?")))
    report = best_feature_splits(samples)
    return {
        **report,
        "branch": branch,
        "cases_analyzed": analyzed_cases,
        "cases_skipped": skipped_cases,
        "sample_count": len(samples),
        "feature_version": 1,
    }


@app.post("/admin/screenshot-tests/preview")
async def preview_screenshot_test(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    require_admin(authorization)
    if Path(file.filename or "").suffix.lower() != ".png":
        raise HTTPException(status_code=400, detail="Загрузи оригинальный PNG-скриншот")
    data = await file.read()
    if not data or len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PNG должен быть не пустым и не больше 15 МБ")
    try:
        with Image.open(BytesIO(data)) as opened:
            if opened.format != "PNG":
                raise ValueError("not png")
            opened.load()
            size = [opened.width, opened.height]
            image = opened.convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Не удалось прочитать PNG-скриншот") from exc
    if image.width * image.height > 30_000_000:
        raise HTTPException(status_code=400, detail="Разрешение превышает 30 мегапикселей")
    upload_id = uuid.uuid4().hex
    (TEMP_DIR / f"screenshot-fixture-{upload_id}.png").write_bytes(data)
    result = scan_screenshot(image, save_previews=False)
    # Match each detected crop against saved card hashes so the fixture editor can
    # show the known icon/name next to its editable label.
    enriched_cards = _enrich_admin_scan(result.get("cards", []), load_db())
    return {
        "upload_id": upload_id,
        "image_url": f"/admin/screenshot-tests/upload/{upload_id}",
        "image_size": size,
        "cards": [
            {
                "box": c["box"],
                "quantity": c.get("quantity", 1),
                "matched": bool(c.get("matched")),
                "card_id": c.get("id"),
                "card_name": c.get("name", ""),
                "icon": c.get("icon", ""),
                "match_distance": c.get("distance"),
            }
            for c in enriched_cards
        ],
    }


@app.get("/admin/screenshot-tests/upload/{upload_id}")
def get_screenshot_test_upload(upload_id: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    path = TEMP_DIR / f"screenshot-fixture-{_safe_screenshot_upload_id(upload_id)}.png"
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Временный скриншот истёк. Загрузи его повторно.")
    return Response(path.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/admin/screenshot-tests/image/{case_id}")
def get_screenshot_test_image(case_id: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    branch = _screenshot_test_branch()
    case = next((c for c in _screenshot_test_manifest(branch)["cases"] if str(c.get("id")) == case_id), None)
    if not case:
        raise HTTPException(status_code=404, detail="Эталонный скриншот не найден")
    path = case.get("image_path", "")
    if not isinstance(path, str) or not path.startswith(SCREENSHOT_TEST_IMAGES_PATH + "/") or ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="Некорректный путь изображения")
    data = _github_read_bytes(path, branch)
    if data is None:
        raise HTTPException(status_code=404, detail="Файл скриншота отсутствует в GitHub")
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/admin/screenshot-tests")
def save_screenshot_test(data: ScreenshotFixtureSaveSchema, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    case_id = data.id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}", case_id):
        raise HTTPException(status_code=400, detail="ID: от 3 до 80 символов, латиница, цифры, _ и -")
    if not data.cards or len(data.cards) > 100:
        raise HTTPException(status_code=400, detail="Добавь от 1 до 100 карт")
    upload_path = TEMP_DIR / f"screenshot-fixture-{_safe_screenshot_upload_id(data.upload_id)}.png"
    if not upload_path.is_file():
        raise HTTPException(status_code=410, detail="Временный скриншот истёк. Загрузи его повторно.")
    image_data = upload_path.read_bytes()
    try:
        with Image.open(BytesIO(image_data)) as image:
            image.load()
            width, height = image.size
            if image.format != "PNG":
                raise ValueError("not png")
            source_image = image.convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Некорректный PNG") from exc
    for i, card in enumerate(data.cards, 1):
        if len(card.box) != 4:
            raise HTTPException(status_code=400, detail=f"Карта {i}: box должен содержать x, y, ширину и высоту")
        x, y, w, h = card.box
        if x < 0 or y < 0 or w < 20 or h < 20 or x + w > width or y + h > height:
            raise HTTPException(status_code=400, detail=f"Карта {i}: рамка выходит за границы или слишком мала")
        if card.expected_quantity < 1 or card.expected_quantity > 999:
            raise HTTPException(status_code=400, detail=f"Карта {i}: количество должно быть 1–999")
        if card.state not in {"owned", "unowned"}:
            raise HTTPException(status_code=400, detail=f"Карта {i}: неизвестное состояние")
        if card.identity_verified and card.state != "owned":
            raise HTTPException(status_code=400, detail=f"Карта {i}: нельзя подтверждать ID карты вне инвентаря")
    branch = _screenshot_test_branch()
    payload = _screenshot_test_manifest(branch)
    if any(str(c.get("id")) == case_id for c in payload["cases"]):
        raise HTTPException(status_code=409, detail="Такой ID уже существует")
    image_path = f"{SCREENSHOT_TEST_IMAGES_PATH}/{case_id}.png"
    payload["version"] = 1
    labelled_cards = []
    for card in data.cards:
        x, y, w, h = card.box
        labelled = card.model_dump()
        labelled["visual_features"] = extract_card_visual_features(
            source_image.crop((x, y, x + w, y + h))
        )
        labelled_cards.append(labelled)
    payload["cases"].append({"id": case_id, "title": data.title.strip()[:120], "image_path": image_path,
        "source_screenshot_size": [width, height], "cards": labelled_cards,
        "image_sha256": hashlib.sha256(image_data).hexdigest()})
    files = {image_path: image_data, SCREENSHOT_TEST_CASES_PATH: json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"}
    commit = _github_commit_files(files, f"Add screenshot quality fixture {case_id}", branch)
    upload_path.unlink(missing_ok=True)
    return {"ok": True, "id": case_id, "branch": branch, "git_commit": commit["sha"]}



@app.post("/admin/screenshot-tests/{case_id}/identity")
def update_screenshot_test_identity(
    case_id: str,
    data: ScreenshotFixtureIdentityUpdateSchema,
    authorization: str | None = Header(default=None),
):
    """Save manually verified catalogue IDs without rewriting the source screenshot or labels."""
    require_admin(authorization)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,79}", case_id):
        raise HTTPException(status_code=400, detail="Некорректный ID эталона")
    branch = _screenshot_test_branch()
    payload = _screenshot_test_manifest(branch)
    case = next((item for item in payload["cases"] if str(item.get("id")) == case_id), None)
    if case is None:
        raise HTTPException(status_code=404, detail="Эталон не найден")
    cards = case.get("cards")
    if not isinstance(cards, list) or len(cards) != len(data.cards):
        raise HTTPException(status_code=400, detail="Количество меток не совпадает с эталоном")
    catalog = load_db()
    for index, (existing, update) in enumerate(zip(cards, data.cards), 1):
        if not isinstance(existing, dict):
            raise HTTPException(status_code=400, detail=f"Некорректная метка карты {index}")
        verified = bool(update.identity_verified)
        card_id = update.card_id.strip()
        if verified and existing.get("state", "owned") != "owned":
            raise HTTPException(status_code=400, detail=f"Карта {index}: нельзя подтверждать идентичность карты вне инвентаря")
        if verified and card_id and card_id not in catalog:
            raise HTTPException(status_code=400, detail=f"Карта {index}: ID {card_id} отсутствует в каталоге")
        existing["card_id"] = card_id
        existing["identity_verified"] = verified
        if card_id:
            existing["card_name"] = str(catalog.get(card_id, {}).get("name") or existing.get("card_name") or card_id)
    manifest_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    commit = _github_commit_files(
        {SCREENSHOT_TEST_CASES_PATH: manifest_bytes},
        f"Verify card identities in screenshot fixture {case_id}",
        branch,
    )
    return {
        "ok": True,
        "id": case_id,
        "branch": branch,
        "verified_cards": sum(1 for card in cards if card.get("identity_verified") is True),
        "git_commit": commit["sha"],
    }


@app.delete("/admin/screenshot-tests/{case_id}")
def delete_screenshot_test(case_id: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    branch = _screenshot_test_branch()
    payload = _screenshot_test_manifest(branch)
    case = next((c for c in payload["cases"] if str(c.get("id")) == case_id), None)
    if not case:
        raise HTTPException(status_code=404, detail="Эталон не найден")
    payload["cases"] = [c for c in payload["cases"] if str(c.get("id")) != case_id]
    files: dict[str, bytes | None] = {SCREENSHOT_TEST_CASES_PATH: json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"}
    path = case.get("image_path", "")
    if isinstance(path, str) and path.startswith(SCREENSHOT_TEST_IMAGES_PATH + "/") and ".." not in Path(path).parts:
        files[path] = None
    commit = _github_commit_files(files, f"Delete screenshot quality fixture {case_id}", branch)
    return {"ok": True, "id": case_id, "branch": branch, "git_commit": commit["sha"]}


@app.post("/admin/screenshot-tests/run")
def run_screenshot_tests(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    if not SCREENSHOT_TEST_RUN_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Проверка скриншотов уже выполняется")
    try:
        with _ResourceMonitor() as resource_monitor:
            return _run_screenshot_tests(resource_monitor)
    finally:
        SCREENSHOT_TEST_RUN_LOCK.release()


def _run_screenshot_tests(resource_monitor: _ResourceMonitor) -> dict[str, Any]:
    manifest_started = time.perf_counter()
    branch = _screenshot_test_branch()
    cases = _screenshot_test_manifest(branch).get("cases", [])
    manifest_fetch_ms = round((time.perf_counter() - manifest_started) * 1000, 1)
    start_time = resource_monitor._start_time
    results = []
    catalog_started = time.perf_counter()
    catalog = load_db()
    catalog_load_ms = round((time.perf_counter() - catalog_started) * 1000, 1)
    processed = 0
    for case in cases:
        case_before, case_started, case_samples = resource_monitor.start_case()
        case_profile: dict[str, Any] = {}
        case_timings: dict[str, Any] = {}
        errors = []
        if not isinstance(case, dict):
            _, resource_metrics = resource_monitor.finish_case(
                case_before, case_started, case_samples
            )
            error = "Некорректная запись эталона в манифесте"
            results.append({
                "id": "?", "passed": False, "errors": [error],
                "expected_cards": 0, "detected_cards": 0,
                "quantity_matches": 0, "quantity_checked": 0,
                "identity_matches": 0, "identity_checked": 0,
                "profile": {"case_total_ms": round((time.perf_counter() - case_started) * 1000, 1)},
                "timings": {"case_total_ms": round((time.perf_counter() - case_started) * 1000, 1)},
                "cpu_metrics": resource_metrics["cpu"],
                "memory_metrics": resource_metrics["memory"],
                "measurement_limitations": [],
                "status": "error", "error": error, "error_stage": "fixture_manifest",
            })
            continue
        case_id = str(case.get("id", "?"))
        raw_cards = case.get("cards", [])
        if not isinstance(raw_cards, list) or any(not isinstance(card, dict) for card in raw_cards):
            _, resource_metrics = resource_monitor.finish_case(
                case_before, case_started, case_samples
            )
            error = "Некорректный список карт в эталоне"
            results.append({
                "id": case_id, "passed": False, "errors": [error],
                "expected_cards": 0, "detected_cards": 0,
                "quantity_matches": 0, "quantity_checked": 0,
                "identity_matches": 0, "identity_checked": 0,
                "profile": {"case_total_ms": round((time.perf_counter() - case_started) * 1000, 1)},
                "timings": {"case_total_ms": round((time.perf_counter() - case_started) * 1000, 1)},
                "cpu_metrics": resource_metrics["cpu"],
                "memory_metrics": resource_metrics["memory"],
                "measurement_limitations": [],
                "status": "error", "error": error, "error_stage": "fixture_manifest",
            })
            continue
        expected_owned_count = sum(
            1 for card in raw_cards if card.get("state", "owned") == "owned"
        )
        path = case.get("image_path", "")
        if not isinstance(path, str) or not path.startswith(SCREENSHOT_TEST_IMAGES_PATH + "/") or ".." in Path(path).parts:
            data = None
            fetch_error = "Некорректный путь к эталонному изображению"
        else:
            try:
                data = _github_read_bytes(path, branch)
                fetch_error = None
            except Exception as exc:
                data = None
                fetch_error = f"Ошибка загрузки эталона: {type(exc).__name__}"
        case_timings["fetch_ms"] = round((time.perf_counter() - case_started) * 1000, 1)
        if data is None:
            _, resource_metrics = resource_monitor.finish_case(case_before, case_started, case_samples)
            total_ms = round((time.perf_counter() - case_started) * 1000, 1)
            error = fetch_error or "Файл скриншота отсутствует"
            case_profile.update({"case_total_ms": total_ms, "result": "fixture_load_error"})
            case_timings.update({"case_total_ms": total_ms})
            result_entry = {
                "id": case_id, "passed": False, "errors": [error],
                "expected_cards": expected_owned_count, "detected_cards": 0,
                "quantity_matches": 0, "quantity_checked": 0,
                "identity_matches": 0, "identity_checked": 0,
                "profile": case_profile, "timings": case_timings,
                "cpu_metrics": resource_metrics["cpu"], "memory_metrics": resource_metrics["memory"],
                "measurement_limitations": ["Изображение не загружено; показатели включают время получения эталона."],
                "status": "error", "error": error, "error_stage": "fixture_load",
            }
            results.append(result_entry)
            continue
        stage = "decode"
        try:
            load_started = time.perf_counter()
            with Image.open(BytesIO(data)) as source:
                source.load()
                image = source.convert("RGB")
            case_timings["decode_ms"] = round((time.perf_counter() - load_started) * 1000, 1)
            stage = "scan"
            scan_started = time.perf_counter()
            result = scan_screenshot(image, save_previews=False)
            case_timings["scan_ms"] = round((time.perf_counter() - scan_started) * 1000, 1)
            case_profile.update(dict(result.get("profile", {})))
            case_profile["scan_total_ms"] = case_timings["scan_ms"]
        except Exception as exc:
            _, resource_metrics = resource_monitor.finish_case(case_before, case_started, case_samples)
            total_ms = round((time.perf_counter() - case_started) * 1000, 1)
            error = f"Ошибка этапа {stage}: {type(exc).__name__}"
            case_profile.update({"case_total_ms": total_ms, "result": f"{stage}_exception"})
            case_timings.update({"case_total_ms": total_ms})
            result_entry = {
                "id": case_id, "passed": False, "errors": [error],
                "expected_cards": expected_owned_count, "detected_cards": 0,
                "quantity_matches": 0, "quantity_checked": 0,
                "identity_matches": 0,
                "identity_checked": sum(
                    1 for card in case.get("cards", [])
                    if card.get("state", "owned") == "owned"
                    and card.get("identity_verified") is True
                ),
                "profile": case_profile, "timings": case_timings,
                "cpu_metrics": resource_metrics["cpu"], "memory_metrics": resource_metrics["memory"],
                "measurement_limitations": ["Ресурсные пики являются интервальными оценками."],
                "status": "error", "error": error, "error_stage": stage,
            }
            results.append(result_entry)
            continue
        owned = [c for c in raw_cards if c.get("state", "owned") == "owned"]
        detected_result = result.get("cards", [])
        if not isinstance(detected_result, list):
            errors.append("Сканер вернул некорректный список карт")
            detected = []
        else:
            detected = [card for card in detected_result if isinstance(card, dict)]
            if len(detected) != len(detected_result):
                errors.append("Сканер вернул некорректную запись карты")
        matching_started = time.perf_counter()
        try:
            catalog_matches = _enrich_admin_scan(detected, catalog)
            if not isinstance(catalog_matches, list):
                raise TypeError("invalid catalog match result")
        except Exception as exc:
            catalog_matches = []
            errors.append(f"Ошибка сопоставления с каталогом: {type(exc).__name__}")
        case_timings["matching_ms"] = round((time.perf_counter() - matching_started) * 1000, 1)
        quality_started = time.perf_counter()
        if len(owned) != len(detected):
            errors.append(f"Количество карт: ожидалось {len(owned)}, найдено {len(detected)}")
        unmatched = list(detected)
        matched = checked = 0
        identity_matches = 0
        identity_checked = sum(1 for card in owned if card.get("identity_verified") is True)
        identity_details: list[dict[str, Any]] = []
        for expected in owned:
            box = expected.get("box", [])
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(not isinstance(value, int) or isinstance(value, bool) for value in box)
                or box[2] <= 0
                or box[3] <= 0
            ):
                errors.append("Некорректная эталонная рамка")
                continue
            ex, ey, ew, eh = box
            best, best_iou = None, 0.0
            for actual in unmatched:
                actual_box = actual.get("box")
                if (
                    not isinstance(actual_box, list)
                    or len(actual_box) != 4
                    or any(not isinstance(value, int) for value in actual_box)
                    or actual_box[2] <= 0
                    or actual_box[3] <= 0
                ):
                    continue
                ax, ay, aw, ah = actual_box
                ix, iy = max(ex, ax), max(ey, ay)
                iw, ih = max(0, min(ex + ew, ax + aw) - ix), max(0, min(ey + eh, ay + ah) - iy)
                inter = iw * ih
                union = ew * eh + aw * ah - inter
                score = inter / union if union else 0.0
                if score > best_iou:
                    best, best_iou = actual, score
            label = expected.get("card_name") or expected.get("card_id") or "Карта"
            if best is None or best_iou < 0.70:
                errors.append(f"{label}: IoU {best_iou:.2f}, требуется не менее 0.70")
                if expected.get("identity_verified") is True:
                    identity_details.append({
                        "expected_card_id": str(expected.get("card_id") or ""),
                        "expected_card_name": str(expected.get("card_name") or label),
                        "matched_card_id": None, "matched": False, "decision": "unmatched_other",
                        "match_score": None, "runner_up_id": None, "runner_up_score": None, "margin": None,
                        "strong_count": 0, "acceptable_count": 0, "color_ratio": None, "color_penalty": None,
                        "distances_to_expected": {k: None for k in ("full_phash", "full_dhash", "visual_phash", "visual_dhash", "frame_colorhash_bits", "frame_colorhash_ratio")},
                        "distances_to_matched": None, "distances_to_runner_up": None,
                        "crop": {"box": None, "width": None, "height": None},
                        "quantity": {"expected": int(expected.get("expected_quantity", 1) or 1), "detected": None},
                        "has_counter_evidence": False, "hash_reuse": False,
                    })
                continue
            actual_index = next((index for index, candidate in enumerate(detected) if candidate is best), None)
            unmatched.remove(best)
            ax, ay, aw, ah = best["box"]
            ix, iy = max(ex, ax), max(ey, ay)
            inter = max(0, min(ex + ew, ax + aw) - ix) * max(0, min(ey + eh, ay + ah) - iy)
            coverage = inter / (ew * eh) if ew * eh else 0
            precision = inter / (aw * ah) if aw * ah else 0
            if coverage < 0.90: errors.append(f"{label}: покрытие карты {coverage:.0%}, требуется ≥ 90%")
            if precision < 0.75: errors.append(f"{label}: точность кропа {precision:.0%}, требуется ≥ 75%")
            checked += 1
            try:
                actual_quantity = int(best.get("quantity", 1))
                expected_quantity = int(expected.get("expected_quantity", 1))
            except (TypeError, ValueError):
                errors.append(f"{label}: некорректное количество в результате сканирования или эталоне")
            else:
                if actual_quantity == expected_quantity:
                    matched += 1
                else:
                    errors.append(
                        f"{label}: ожидалось {expected_quantity}, найдено {actual_quantity}"
                    )
            if expected.get("identity_verified") is True:
                expected_id = str(expected.get("card_id") or "").strip()
                actual_identity = catalog_matches[actual_index] if actual_index is not None and actual_index < len(catalog_matches) and isinstance(catalog_matches[actual_index], dict) else {}
                actual_id = str(actual_identity.get("id") or "").strip() if actual_identity.get("matched") else ""
                scan_hashes = clean_hashes(best.get("hashes"))
                raw_hashes = clean_hashes(best.get("raw_hashes"))
                detail = _match_card_detailed(scan_hashes, catalog)
                if raw_hashes != scan_hashes:
                    raw_detail = _match_card_detailed(raw_hashes, catalog)
                    if raw_detail.get("id") is not None and (detail.get("score") is None or raw_detail["score"] < detail["score"]):
                        detail, scan_hashes = raw_detail, raw_hashes
                runner_id = detail.get("runner_up_id")
                expected_distances = _identity_hash_distances(scan_hashes, catalog.get(expected_id))
                matched_info = catalog.get(actual_id) if actual_id else None
                decision = "matched_ok" if actual_id and actual_id == expected_id else "wrong_id" if actual_id else detail.get("reason", "unmatched_other")
                if decision not in ("matched_ok", "wrong_id", "unmatched_no_candidates", "unmatched_low_strong", "unmatched_low_margin", "unmatched_color_reject", "unmatched_outlier_hash"):
                    decision = "unmatched_other"
                if decision == "matched_ok":
                    identity_matches += 1
                else:
                    errors.append(f"{label}: ID карты — ожидалось {expected_id or 'неизвестная карта'}, распознано {actual_id or 'совпадение не найдено'}")
                score_value = detail.get("score") if actual_id else detail.get("candidate_score")
                identity_details.append({
                    "expected_card_id": expected_id, "expected_card_name": str(expected.get("card_name") or label),
                    "matched_card_id": actual_id or None, "matched": bool(actual_id), "decision": decision,
                    "match_score": round(float(score_value), 3) if score_value is not None else None,
                    "runner_up_id": runner_id,
                    "runner_up_score": round(float(detail["runner_up_score"]), 3) if detail.get("runner_up_score") is not None else None,
                    "margin": round(float(detail["margin"]), 3) if detail.get("margin") is not None and math.isfinite(float(detail["margin"])) else None,
                    "strong_count": int(detail.get("strong_count", 0)), "acceptable_count": int(detail.get("acceptable_count", 0)),
                    "color_ratio": round(float(detail["color_ratio"]), 4) if detail.get("color_ratio") is not None else None,
                    "color_penalty": round(float(detail["color_penalty"]), 4) if detail.get("color_penalty") is not None else None,
                    "distances_to_expected": expected_distances,
                    "distances_to_matched": _identity_hash_distances(scan_hashes, matched_info) if matched_info else None,
                    "distances_to_runner_up": _identity_hash_distances(scan_hashes, catalog.get(runner_id)) if runner_id else None,
                    "crop": {"box": [ax, ay, aw, ah], "width": aw, "height": ah},
                    "quantity": {"expected": int(expected.get("expected_quantity", 1) or 1), "detected": actual_quantity},
                    "has_counter_evidence": _has_counter_evidence(float(best.get("quantity_confidence", 0) or 0), best.get("counter_debug", {}) if isinstance(best.get("counter_debug"), dict) else {}),
                    "hash_reuse": bool(scan_hashes == raw_hashes),
                })
        case_timings["quality_check_ms"] = round((time.perf_counter() - quality_started) * 1000, 1)
        case_profile["quality_passed"] = not errors
        case_profile["quantity_matches"] = matched
        case_profile["quantity_checked"] = checked
        case_profile["identity_matches"] = identity_matches
        case_profile["identity_checked"] = identity_checked
        identity_decision_counts = _build_identity_stats([{"identity_details": identity_details}])["by_decision"]
        processed += 1
        _, resource_metrics = resource_monitor.finish_case(case_before, case_started, case_samples)
        result_started = time.perf_counter()
        result_entry = {
            "id": case_id, "passed": not errors, "errors": errors,
            "expected_cards": len(owned), "detected_cards": len(detected),
            "quantity_matches": matched, "quantity_checked": checked,
            "identity_matches": identity_matches, "identity_checked": identity_checked,
            "identity_details": identity_details, "identity_decision_counts": identity_decision_counts,
            "profile": case_profile, "timings": case_timings,
            "cpu_metrics": resource_metrics["cpu"], "memory_metrics": resource_metrics["memory"],
            "measurement_limitations": ["Пики CPU и памяти являются интервальными оценками; короткие пики могут быть пропущены."],
            "status": "passed" if not errors else "failed",
        }
        case_timings["result_assembly_ms"] = round((time.perf_counter() - result_started) * 1000, 1)
        case_timings["case_total_ms"] = round((time.perf_counter() - case_started) * 1000, 1)
        case_timings["scanner_total_ms"] = case_profile.get("total_ms")
        case_profile["case_total_ms"] = case_timings["case_total_ms"]
        results.append(result_entry)
    resource_monitor.stop()
    passed = sum(x["passed"] for x in results)
    test_duration_ms = round(resource_monitor.summary()["duration_seconds"] * 1000, 1)
    report_id = uuid.uuid4().hex
    aggregate_timing_values: dict[str, list[float]] = {}
    aggregate_cpu_values: dict[str, list[float]] = {}
    aggregate_memory_values: dict[str, list[float]] = {}
    for item in results:
        for key, value in item.get("timings", {}).items():
            if isinstance(value, (int, float)):
                aggregate_timing_values.setdefault(key, []).append(float(value))
        for key, value in item.get("profile", {}).items():
            if key.endswith("_ms") and isinstance(value, (int, float)):
                aggregate_timing_values.setdefault(f"scanner.{key}", []).append(float(value))
        for key in ("cpu_avg_percent", "cpu_peak_percent", "user_cpu_delta", "system_cpu_delta"):
            value = item.get("cpu_metrics", {}).get(key)
            if isinstance(value, (int, float)):
                aggregate_cpu_values.setdefault(key, []).append(float(value))
        for key in ("rss_delta", "sampled_peak_rss_bytes", "vms_after", "container_memory_after"):
            value = item.get("memory_metrics", {}).get(key)
            if isinstance(value, (int, float)):
                aggregate_memory_values.setdefault(key, []).append(float(value))
    aggregate_timing_values["test.manifest_fetch_ms"] = [manifest_fetch_ms]
    aggregate_timing_values["test.catalog_load_ms"] = [catalog_load_ms]

    resource_summary = resource_monitor.summary()
    scan_times = [
        float(item["timings"]["scan_ms"])
        for item in results
        if isinstance(item.get("timings", {}).get("scan_ms"), (int, float))
    ]
    error_count = sum(1 for item in results if item.get("status") == "error" or item.get("error"))
    end_time = datetime.fromisoformat(resource_summary["end_time"])
    total_cpu = resource_summary["cpu"]
    cpu_metrics = {
        "process": total_cpu,
        "container": {
            "cpu_seconds_delta": total_cpu.get("container_cpu_seconds_delta"),
            "cpu_limit": resource_summary["end_state"].get("cpu_limit"),
        },
        "system": None,
        "sampled_peak_percent": resource_summary["cpu_peak_percent"],
        "sampled_average_percent": resource_summary["cpu_sample_avg_percent"],
    }
    memory_metrics = {
        "process": {
            "rss_before": resource_summary["start_state"].get("rss_bytes"),
            "rss_after": resource_summary["end_state"].get("rss_bytes"),
            "rss_delta": _resource_delta(
                resource_summary["start_state"],
                resource_summary["end_state"],
                test_duration_ms / 1000,
            )["rss_delta"],
            "vms_before": resource_summary["start_state"].get("vms_bytes"),
            "vms_after": resource_summary["end_state"].get("vms_bytes"),
            "sampled_peak_rss_bytes": resource_summary["sampled_peak_rss_bytes"],
        },
        "container": {
            "memory_before": resource_summary["start_state"].get("container_memory_bytes"),
            "memory_after": resource_summary["end_state"].get("container_memory_bytes"),
            "sampled_peak_memory_bytes": resource_summary["sampled_peak_container_memory_bytes"],
            "memory_limit_bytes": resource_summary["end_state"].get("memory_limit_bytes"),
        },
    }
    report = {
        "schema_version": 2,
        "metadata": {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_ms": test_duration_ms,
            "run_id": report_id,
            "schema_version": 2,
            "case_count": len(cases),
            "processed_count": processed,
            "passed_count": passed,
            "failed_count": len(results) - passed,
            "error_count": error_count,
            "profiling_mode": "server-side stdlib process and cgroup sampling",
            "timings": {
                "manifest_fetch_ms": manifest_fetch_ms,
                "catalog_load_ms": catalog_load_ms,
                "total_ms": test_duration_ms,
            },
        },
        "environment": {
            "python_version": platform.python_version(),
            "pillow_version": getattr(Image, "__version__", "unknown"),
            "psutil_version": None,
            "os": platform.platform(),
            "platform": platform.system(),
            "git_sha": get_app_commit(),
            "cpu_count": resource_summary["end_state"].get("cpu_count"),
            "available_cpu_count": resource_summary["end_state"].get("available_cpu_count"),
            "cpu_limit": resource_summary["end_state"].get("cpu_limit"),
            "memory_limit": resource_summary["end_state"].get("memory_limit_bytes"),
            "container_info": {
                "memory_limit_bytes": resource_summary["end_state"].get("memory_limit_bytes"),
                "cpu_limit": resource_summary["end_state"].get("cpu_limit"),
            },
            "profiling_source": resource_summary["end_state"].get("metric_source"),
        },
        "summary": {
            "total_cases": len(cases),
            "processed_cases": processed,
            "successful_cases": passed,
            "failed_cases": len(results) - passed,
            "error_cases": error_count,
            "passed_quality_checks": sum(1 for item in results if item.get("passed") is True),
            "failed_quality_checks": sum(1 for item in results if item.get("passed") is False),
            "total_duration_ms": test_duration_ms,
            "manifest_fetch_ms": manifest_fetch_ms,
            "catalog_load_ms": catalog_load_ms,
            "total_scan_time_ms": round(sum(scan_times), 3),
            "avg_scan_time_ms": round(sum(scan_times) / len(scan_times), 3) if scan_times else None,
            "median_scan_time_ms": _aggregate_metric(scan_times)["median"],
            "max_case_duration_ms": max((float(item.get("timings", {}).get("case_total_ms", 0.0)) for item in results if isinstance(item.get("timings"), dict)), default=0.0),
            "peak_rss": resource_summary["sampled_peak_rss_bytes"],
            "peak_cpu": resource_summary["cpu_peak_percent"],
            "identity_stats": _build_identity_stats(results),
        },
        "resource_usage": {
            "start_state": resource_summary["start_state"],
            "end_state": resource_summary["end_state"],
            "rss": memory_metrics["process"],
            "vms": {
                "before": memory_metrics["process"]["vms_before"],
                "after": memory_metrics["process"]["vms_after"],
            },
            "peak_rss": resource_summary["sampled_peak_rss_bytes"],
            "cpu_metrics": cpu_metrics,
            "container_metrics": memory_metrics["container"],
            "monitoring_interval_ms": int(resource_monitor.interval_seconds * 1000),
            "sample_count": resource_summary["sample_count"],
            "limitations": [
                "CPU and memory peaks are sampled; peaks shorter than the sampling interval may be missed.",
                "Process CPU is measured with resource.getrusage; CPU percent may exceed 100% for multithreaded work.",
                "System-wide CPU usage is not collected. Container metrics are null when cgroup counters are unavailable.",
                "No image bytes are stored in the report.",
            ],
            "collection_errors": resource_summary["collection_errors"],
        },
        "aggregate_timings": {key: _aggregate_metric(values) for key, values in aggregate_timing_values.items()},
        "aggregate_resources": {
            "cpu": {key: _aggregate_resource_metric(values) for key, values in aggregate_cpu_values.items()},
            "memory": {key: _aggregate_resource_metric(values) for key, values in aggregate_memory_values.items()},
        },
        "cases": [
            {
                "case_id": item.get("id"),
                "image_path": case.get("image_path") if isinstance(case, dict) else None,
                "image_dimensions": [
                    item.get("profile", {}).get("input_width"),
                    item.get("profile", {}).get("input_height"),
                ] if item.get("profile", {}).get("input_width") is not None else (
                    case.get("source_screenshot_size") if isinstance(case, dict) else None
                ),
                "expected_cards": item.get("expected_cards"),
                "detected_cards": item.get("detected_cards"),
                "quality_result": item.get("passed"),
                "matches": {"quantity": item.get("quantity_matches"), "identity": item.get("identity_matches")},
                "identity_details": item.get("identity_details", []),
                "identity_decision_counts": item.get("identity_decision_counts", {}),
                "mismatches": {"quantity": max(item.get("quantity_checked", 0) - item.get("quantity_matches", 0), 0), "identity": max(item.get("identity_checked", 0) - item.get("identity_matches", 0), 0)},
                "profile": item.get("profile"),
                "timings": item.get("timings"),
                "cpu_metrics": item.get("cpu_metrics"),
                "memory_metrics": item.get("memory_metrics"),
                "status": item.get("status"),
                "errors": item.get("errors", []),
                "error_stage": item.get("error_stage"),
                "error": item.get("error"),
                "measurement_limitations": item.get("measurement_limitations", []),
            }
            for case, item in zip(cases, results)
        ],
    }
    filename = f"scanner-quality-report-{start_time.strftime('%Y-%m-%dT%H-%M-%S')}.json"
    return {
        "count": len(results),
        "processed": processed,
        "passed": passed,
        "failed": len(results) - passed,
        "errors": error_count,
        "results": results,
        "run_id": report_id,
        "profile": report["summary"],
        "report": report,
        "report_filename": filename,
        "created_at": start_time.isoformat(),
        "branch": branch,
        "app_commit": get_app_commit(),
    }


@app.post("/admin/counter-tests/run")
def run_counter_tests(authorization: str | None = Header(default=None)):
    """Run the current counter detector against labelled fixture images; no data is modified."""
    require_admin(authorization)
    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    if not raw:
        raise HTTPException(status_code=404, detail="В рабочей ветке нет tests/fixtures/counter/cases.json")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Файл cases.json содержит некорректный JSON") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise HTTPException(status_code=500, detail="В cases.json отсутствует список cases")
    if not cases:
        return {
            "branch": branch,
            "app_commit": get_app_commit(),
            "run_at": datetime.now(timezone.utc).isoformat(),
            "total": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
            "message": "Нет эталонных тестовых случаев.",
        }

    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case.get("id", "unknown"))[:120]
        expected = case.get("expected_quantity")
        image_path = case.get("image_path")
        result: dict[str, Any] = {
            "id": case_id,
            "card_id": case.get("card_id"),
            "card_name": case.get("card_name"),
            "timestamp": case.get("timestamp"),
            "expected_quantity": expected,
            "image_path": image_path,
            "status": "failed",
            "detected_quantity": None,
            "confidence": None,
            "duration_ms": None,
            "debug": None,
            "error": None,
        }
        try:
            if not isinstance(image_path, str) or not image_path.startswith(COUNTER_TEST_IMAGES_PATH + "/") or ".." in Path(image_path).parts:
                raise ValueError("Недопустимый путь изображения в cases.json")
            if not isinstance(expected, int) or isinstance(expected, bool) or not 1 <= expected <= 999:
                raise ValueError("Эталонное количество должно быть целым числом от 1 до 999")
            # Prefer a local fixture only when its SHA matches the manifest; otherwise read
            # the exact version from GitHub so a stale deployment cannot test old data.
            image_bytes = None
            local_image = Path(image_path)
            expected_sha = case.get("image_sha256")
            if local_image.is_file():
                local_bytes = local_image.read_bytes()
                if not expected_sha or hashlib.sha256(local_bytes).hexdigest() == expected_sha:
                    image_bytes = local_bytes
            if image_bytes is None:
                image_bytes = _github_read_bytes(image_path, branch)
            if not image_bytes:
                raise FileNotFoundError("Эталонное изображение не найдено в рабочей ветке")
            if expected_sha and hashlib.sha256(image_bytes).hexdigest() != expected_sha:
                raise ValueError("SHA-256 изображения не совпадает с cases.json")
            with Image.open(BytesIO(image_bytes)) as source:
                image = source.convert("RGB")
            detector_started = time.perf_counter()
            _, actual, confidence, debug = _detect_counter(image)
            result["duration_ms"] = round((time.perf_counter() - detector_started) * 1000, 2)
            result["detected_quantity"] = actual
            result["confidence"] = confidence
            result["debug"] = debug
            result["status"] = "passed" if actual == expected else "failed"
            if actual != expected:
                result["error"] = f"Ожидалось {expected}, распознано {actual}"
        except Exception as exc:
            result["error"] = str(exc)[:500]
        results.append(result)

    passed = sum(1 for item in results if item["status"] == "passed")
    failed = len(results) - passed
    return {
        "branch": branch,
        "app_commit": get_app_commit(),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "results": results,
        "message": "Все тесты пройдены." if failed == 0 else f"Не пройдено тестов: {failed}.",
    }


@app.get("/admin/counter-tests")
async def get_counter_tests(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    try:
        payload = json.loads(raw) if raw else {"version": 1, "cases": []}
        if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
            raise ValueError("Некорректная структура cases.json")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Файл тестовых случаев в GitHub содержит некорректный формат") from exc
    return {
        "branch": branch,
        "count": len(payload["cases"]),
        "recent": [
            {
                "id": item.get("id"),
                "timestamp": item.get("timestamp"),
                "card_name": item.get("card_name"),
                "card_id": item.get("card_id"),
                "expected_quantity": item.get("expected_quantity"),
                "detected_quantity": item.get("detected_quantity"),
                "correct": item.get("correct"),
                "image_path": item.get("image_path"),
                "image_url": f"/admin/counter-tests/image/{quote(str(item.get('id', '')), safe='')}",
                "commit_url": item.get("commit_url"),
            }
            for item in reversed(payload["cases"])
        ],
    }


@app.get("/admin/counter-tests/image/{case_id}")
async def get_counter_test_image(case_id: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    try:
        payload = json.loads(raw) if raw else {"cases": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Файл cases.json содержит некорректный JSON") from exc
    case = next((item for item in payload.get("cases", []) if item.get("id") == case_id), None)
    if case is None:
        raise HTTPException(status_code=404, detail="Эталонный тест не найден")
    image_path = case.get("image_path")
    if not isinstance(image_path, str) or not image_path.startswith(COUNTER_TEST_IMAGES_PATH + "/") or ".." in Path(image_path).parts:
        raise HTTPException(status_code=400, detail="Недопустимый путь изображения")
    image_bytes = _github_read_bytes(image_path, branch)
    if not image_bytes:
        raise HTTPException(status_code=404, detail="Изображение эталонного теста не найдено")
    media_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg" if image_path.lower().endswith((".jpg", ".jpeg")) else "image/webp"
    return Response(content=image_bytes, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})


@app.patch("/admin/counter-tests/{case_id}")
async def update_counter_test(case_id: str, expected_quantity: int, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    if not 1 <= expected_quantity <= 999:
        raise HTTPException(status_code=400, detail="Эталонное количество должно быть от 1 до 999")
    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    try:
        payload = json.loads(raw) if raw else {"version": 1, "cases": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Файл cases.json содержит некорректный JSON") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise HTTPException(status_code=500, detail="В cases.json отсутствует список cases")
    case = next((item for item in cases if item.get("id") == case_id), None)
    if case is None:
        raise HTTPException(status_code=404, detail="Эталонный тест не найден")
    case["expected_quantity"] = expected_quantity
    case["correct"] = case.get("detected_quantity") == expected_quantity
    files = {COUNTER_TEST_CASES_PATH: json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"}
    commit = _github_commit_files(files, f"Update counter fixture label {case_id}", branch)
    return {"ok": True, "id": case_id, "expected_quantity": expected_quantity, "branch": branch, "git_commit": commit["sha"]}


@app.delete("/admin/counter-tests/{case_id}")
async def delete_counter_test(case_id: str, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    try:
        payload = json.loads(raw) if raw else {"version": 1, "cases": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Файл cases.json содержит некорректный JSON") from exc
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise HTTPException(status_code=500, detail="В cases.json отсутствует список cases")
    index = next((i for i, item in enumerate(cases) if item.get("id") == case_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="Эталонный тест не найден")
    removed = cases.pop(index)
    image_path = removed.get("image_path")
    files: dict[str, bytes | None] = {
        COUNTER_TEST_CASES_PATH: json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    }
    if isinstance(image_path, str) and image_path.startswith(COUNTER_TEST_IMAGES_PATH + "/") and ".." not in Path(image_path).parts:
        files[image_path] = None
    commit = _github_commit_files(files, f"Delete counter fixture {case_id}", branch)
    return {"ok": True, "id": case_id, "branch": branch, "git_commit": commit["sha"]}


@app.post("/admin/counter-tests")
async def save_counter_tests_batch(
    data: CounterTestBatchSchema,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    if not data.cases or len(data.cases) > 50:
        raise HTTPException(status_code=400, detail="Отметь от 1 до 50 карт с известным количеством")
    branch = _counter_test_branch()
    raw = _github_read_text(COUNTER_TEST_CASES_PATH, branch)
    try:
        payload = json.loads(raw) if raw else {"version": 1, "cases": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Не удалось прочитать cases.json из рабочей ветки") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        payload = {"version": 1, "cases": []}

    timestamp = datetime.now(timezone.utc)
    files: dict[str, bytes] = {}
    new_cases = []
    total_bytes = 0
    for index, item in enumerate(data.cases):
        if item.expected_quantity < 1 or item.expected_quantity > 999:
            raise HTTPException(status_code=400, detail="Реальное количество должно быть от 1 до 999")
        filename = Path(item.preview.split("?", 1)[0]).name
        if not filename.startswith("counter-fixture-") or Path(filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(status_code=400, detail="Повторите сканирование, чтобы получить оригинальный кроп карты")
        local_path = TEMP_DIR / filename
        if not local_path.is_file():
            raise HTTPException(status_code=410, detail="Временный кроп уже удалён. Повторите сканирование.")
        image_bytes = local_path.read_bytes()
        total_bytes += len(image_bytes)
        if not image_bytes or len(image_bytes) > 8 * 1024 * 1024 or total_bytes > 15 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Размер одного изображения должен быть до 8 МБ, общий размер пакета — до 15 МБ")
        image_sha = hashlib.sha256(image_bytes).hexdigest()
        case_time = datetime.now(timezone.utc)
        case_id = f"{case_time.strftime('%Y%m%dT%H%M%S%f')}-{image_sha[:10]}"
        relative_image = f"{COUNTER_TEST_IMAGES_PATH}/{case_id}.png" if Path(filename).suffix.lower() == ".png" else f"{COUNTER_TEST_IMAGES_PATH}/{case_id}{Path(filename).suffix.lower()}"
        try:
            with Image.open(BytesIO(image_bytes)) as fixture_image:
                detector_input_size = list(fixture_image.size)
                detector_input_format = fixture_image.format
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Временное изображение теста повреждено") from exc
        counter_debug = item.counter_debug if isinstance(item.counter_debug, dict) else {}
        case = {
            "id": case_id,
            "timestamp": case_time.isoformat(),
            "app_commit": get_app_commit(),
            "card_id": item.card_id,
            "card_name": item.card_name,
            "row": item.row,
            "col": item.col,
            "card_box_in_screenshot": item.box,
            "source_screenshot_size": item.image_size,
            "detector_input_size": detector_input_size,
            "detector_input_format": detector_input_format,
            "image_path": relative_image,
            "image_sha256": image_sha,
            "expected_quantity": item.expected_quantity,
            "detected_quantity": item.detected_quantity,
            "correct": item.expected_quantity == item.detected_quantity,
            "quantity_confidence": item.quantity_confidence,
            "scan_duration_ms": item.scan_duration_ms,
            "counter_debug": counter_debug,
        }
        new_cases.append(case)
        files[relative_image] = image_bytes
    payload.setdefault("version", 1)
    payload["cases"].extend(new_cases)
    payload["cases"] = payload["cases"][-500:]
    files[COUNTER_TEST_CASES_PATH] = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    all_correct = sum(1 for case in new_cases if case["correct"])
    message = f"Add {len(new_cases)} labelled counter test case(s) ({all_correct}/{len(new_cases)} matched)"
    commit = _github_commit_files(files, message, branch)
    for case in new_cases:
        SCAN_DIAGNOSTIC_LOGS.append({
            "timestamp": case["timestamp"],
            "event": "counter_test_labeled",
            "app_commit": case["app_commit"],
            "git_commit": commit["sha"],
            "branch": branch,
            "case_id": case["id"],
            "expected_quantity": case["expected_quantity"],
            "detected_quantity": case["detected_quantity"],
            "correct": case["correct"],
            "image_path": case["image_path"],
            "counter_debug": case["counter_debug"],
        })
    return {
        "status": "saved",
        "cases": new_cases,
        "saved_count": len(new_cases),
        "correct_count": all_correct,
        "git_commit": commit["sha"],
        "commit_url": commit["url"],
        "branch": branch,
        "count": len(payload["cases"]),
    }


@app.get("/admin/temp-files")
async def list_temp_files(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    files = []
    for path in sorted(TEMP_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file():
            files.append({
                "name": path.name,
                "url": f"/temp-images/{path.name}",
                "size": path.stat().st_size,
            })
    return files


@app.delete("/admin/temp-files")
async def delete_temp_files(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    deleted = 0
    for path in TEMP_DIR.glob("*"):
        if path.is_file():
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    return {"status": "ok", "deleted": deleted}


@app.get("/me/cards")
async def get_my_cards(init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    auth = _validate_telegram_init_data(init_data)
    user = auth["user"]
    get_or_create_user(auth["id"], username=str(user.get("username") or ""), first_name=str(user.get("first_name") or ""), last_name=str(user.get("last_name") or ""))
    return {
        "registered": True,
        "telegram_id": auth["id"],
        "user": {
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
        },
        "cards": get_user_cards(auth["id"]),
        "desired_cards": get_desired_cards(auth["id"]),
    }


@app.put("/me/desired-cards")
async def update_desired_cards(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    card_ids = data.get("card_ids", [])
    if not isinstance(card_ids, list):
        raise HTTPException(status_code=400, detail="card_ids должен быть массивом")
    if len(card_ids) > 200:
        raise HTTPException(status_code=400, detail="Слишком много желаемых карт")
    get_or_create_user(auth["id"])
    desired = set_desired_cards(auth["id"], card_ids)
    return {"status": "ok", "desired_cards": desired}


def _send_telegram_message(chat_id: int, text: str) -> None:
    """Send a Telegram Bot API message without exposing bot credentials in errors."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN не задан на сервере")

    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Never return or log the URL, because it contains the bot token.
        raise HTTPException(
            status_code=502,
            detail="Telegram не смог доставить уведомление. Возможно, пользователь ещё не запускал бота.",
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Не удалось отправить уведомление через Telegram. Попробуйте позже.",
        ) from exc

    if not isinstance(result, dict) or not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail="Telegram не смог доставить уведомление. Возможно, пользователь ещё не запускал бота.",
        )


def _card_icon_public_url(icon: Any) -> str:
    """Return a public URL only for card icons hosted by this application."""
    path = str(icon or "").strip()
    if not path.startswith("/static/cards/") or ".." in path.split("/"):
        return ""
    return f"{get_public_base_url()}{path}"


def _send_telegram_media_group(chat_id: int, media: list[dict[str, str]]) -> None:
    """Send card icons as a Telegram album. The caller may fall back to text-only."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN не задан на сервере")
    if not 2 <= len(media) <= 10:
        return

    payload = json.dumps({"chat_id": chat_id, "media": media}, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/sendMediaGroup",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Не удалось отправить изображения карт через Telegram.",
        ) from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise HTTPException(status_code=502, detail="Telegram не смог отправить изображения карт")


@app.post("/me/trade-matches/{target_telegram_id}/contact")
async def contact_trade_match(
    target_telegram_id: int,
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    """Create a persistent trade request and notify the recipient through the bot."""
    if target_telegram_id <= 0:
        raise HTTPException(status_code=400, detail="Некорректный Telegram ID")

    auth = _validate_telegram_init_data(init_data)
    sender = auth["user"]
    get_or_create_user(
        auth["id"],
        username=str(sender.get("username") or ""),
        first_name=str(sender.get("first_name") or ""),
        last_name=str(sender.get("last_name") or ""),
    )
    match = next(
        (item for item in get_trade_matches(auth["id"]) if int(item["telegram_id"]) == target_telegram_id),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Подходящий обмен с этим игроком не найден")

    request = create_trade_request_for_match(auth["id"], target_telegram_id, match)
    return {
        "status": "created",
        "request_id": request["id"],
        "notification_delivered": bool(request.get("notification_delivered")),
        "message": (
            "Заявка сохранена и уведомление отправлено"
            if request.get("notification_delivered")
            else "Заявка сохранена. Игрок увидит её в разделе «Обмены», когда откроет приложение"
        ),
    }


@app.get("/me/trade-matches")
async def get_my_trade_matches(init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    """Return reciprocal trade suggestions for the authenticated Telegram user."""
    auth = _validate_telegram_init_data(init_data)
    user = auth["user"]
    get_or_create_user(auth["id"], username=str(user.get("username") or ""), first_name=str(user.get("first_name") or ""), last_name=str(user.get("last_name") or ""))
    matches = get_trade_matches(auth["id"])
    return {"matches": matches, "count": len(matches)}


@app.delete("/me/desired-cards/{card_id}")
async def delete_desired_card(
    card_id: str,
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    get_or_create_user(auth["id"])
    current = [c["id"] for c in get_desired_cards(auth["id"]) if c["id"] != card_id]
    desired = set_desired_cards(auth["id"], current)
    return {"status": "ok", "desired_cards": desired}


@app.delete("/me/collection")
async def clear_my_collection(
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    get_or_create_user(auth["id"])
    clear_user_cards(auth["id"])
    return {"status": "ok", "cards": get_user_cards(auth["id"])}


@app.post("/me/inventory/add-zero")
async def add_zero_inventory_cards(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    """Add selected master cards to the user's inventory with quantity 0."""
    auth = _validate_telegram_init_data(init_data)
    card_ids = data.get("card_ids", [])
    if not isinstance(card_ids, list):
        raise HTTPException(status_code=400, detail="card_ids должен быть массивом")

    get_or_create_user(auth["id"])
    normalized = [str(card_id).strip() for card_id in card_ids if str(card_id).strip()]
    if len(normalized) > 200:
        raise HTTPException(status_code=400, detail="Слишком много карт за один запрос")

    added = add_zero_cards(auth["id"], normalized)
    return {
        "status": "ok",
        "added": added,
        "cards": get_user_cards(auth["id"]),
    }


def _normalize_collection_updates(cards: list[Any]) -> list[tuple[str, int]]:
    """Normalize inventory updates and make duplicate card IDs deterministic.

    A card is represented once in the album and its badge carries the quantity.
    If image detection reports the same stable card ID more than once, keep the
    greatest reported quantity instead of letting the last row silently win.
    Repeating the same scan therefore remains an absolute, idempotent update.
    """
    quantities: dict[str, int] = {}
    for item in cards:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        card_id = str(item["id"]).strip()
        if not card_id:
            continue
        try:
            quantity = max(0, min(999, int(item.get("quantity", 0) or 0)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Некорректное количество карты") from exc
        quantities[card_id] = max(quantities.get(card_id, 0), quantity)
    return list(quantities.items())


@app.post("/me/collection/confirm")
async def confirm_my_collection(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    cards = data.get("cards", [])
    if not isinstance(cards, list):
        raise HTTPException(status_code=400, detail="cards должен быть массивом")
    get_or_create_user(auth["id"])
    updates = _normalize_collection_updates(cards)
    set_user_card_quantities(auth["id"], updates)
    saved = len(updates)
    return {"status": "ok", "saved": saved, "cards": get_user_cards(auth["id"])}


@app.post("/me/collection")
async def save_my_collection(
    data: dict[str, Any],
    init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    auth = _validate_telegram_init_data(init_data)
    cards = data.get("cards", [])
    if not isinstance(cards, list):
        raise HTTPException(status_code=400, detail="cards должен быть массивом")
    get_or_create_user(auth["id"])
    saved = 0
    for item in cards:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        quantity = max(0, int(item.get("quantity", 0) or 0))
        set_user_card_quantity(auth["id"], str(item["id"]), quantity)
        saved += 1
    return {"status": "ok", "saved": saved, "cards": get_user_cards(auth["id"]) }


@app.post("/scan")
async def scan(file: UploadFile = File(...), init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    request_started = time.perf_counter()
    auth = _validate_telegram_init_data(init_data) if init_data else None
    decode_started = time.perf_counter()
    upload_profile: dict[str, Any] = {}
    image = await _read_image(file, upload_profile)
    upload_decode_ms = (time.perf_counter() - decode_started) * 1000
    result = scan_screenshot(image, save_previews=True)
    matching_started = time.perf_counter()
    catalog_load_started = time.perf_counter()
    db = load_db()
    catalog_load_ms = (time.perf_counter() - catalog_load_started) * 1000

    matches = []
    matching_profile: dict[str, int] = {}
    matching_evaluation_started = time.perf_counter()
    for card in result["cards"]:
        best_id, best_distance = _match_card(card["hashes"], db, matching_profile)
        raw_hashes = card.get("raw_hashes", {})
        if raw_hashes != card["hashes"]:
            raw_id, raw_distance = _match_card(raw_hashes, db, matching_profile)
            if raw_id is not None and raw_distance < best_distance:
                best_id, best_distance = raw_id, raw_distance
        matched = (
            best_id is not None
            and best_distance <= float(os.getenv("CARD_MATCH_THRESHOLD", "18"))
        )
        if matched:
            info = db[best_id]
            matches.append({
                "id": best_id,
                "name": info.get("name", best_id),
                "rarity": info.get("rarity", 1),
                "distance": round(best_distance, 2),
                "icon": info.get("icon", ""),
                "preview": card.get("preview", ""),
                "quantity": int(card.get("quantity", 1) or 1),
                "duplicates": int(card.get("duplicates", 0) or 0),
                "row": card["row"],
                "col": card["col"],
            })
        else:
            matches.append({
                "id": None,
                "name": "Неизвестная карта",
                "rarity": None,
                "distance": None,
                "icon": "",
                "preview": card.get("preview", ""),
                "quantity": int(card.get("quantity", 1) or 1),
                "duplicates": int(card.get("duplicates", 0) or 0),
                "row": card["row"],
                "col": card["col"],
            })
    matching_evaluation_ms = (time.perf_counter() - matching_evaluation_started) * 1000
    matching_ms = (time.perf_counter() - matching_started) * 1000
    if auth:
        get_or_create_user(auth["id"])

    profile = dict(result.get("profile", {}))
    profile.update(upload_profile)
    profile["upload_decode_ms"] = round(upload_decode_ms, 1)
    profile["catalog_load_ms"] = round(catalog_load_ms, 1)
    profile["matching_ms"] = round(matching_ms, 1)
    profile["matching_evaluation_ms"] = round(matching_evaluation_ms, 1)
    profile.update(matching_profile)
    profile["matched_count"] = sum(card.get("id") is not None for card in matches)
    profile["server_total_ms"] = round((time.perf_counter() - request_started) * 1000, 1)
    return {"cards": matches, "count": len(matches), "authenticated": bool(auth), "profile": profile}


@app.post("/admin/save-card")
async def save_card(
    data: CardSaveSchema,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    hashes = clean_hashes(data.hashes)
    legacy_hash = data.hash if valid_hash(data.hash) else None

    if not hashes and legacy_hash:
        hashes = {"full_dhash": legacy_hash}

    if not hashes:
        raise HTTPException(status_code=400, detail="Не удалось получить валидные hashes")

    # The scan preview is already cropped to the card. Reuse it as the
    # permanent icon so adding a card never requires uploading the same image twice.
    card_image = _load_admin_preview(data.preview)

    db = load_db()
    requested_id = data.id.strip() if data.id else ""
    if not requested_id and not data.force_new:
        duplicate = _find_duplicate_card_for_save(data.name, hashes, db)
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Похоже, эта карта уже есть в каталоге. Откройте существующую карту вместо создания новой.",
                    "duplicate": duplicate,
                },
            )

    card_id = requested_id or _next_card_id(db)
    old = db.get(card_id, {})
    record = {
        "name": data.name,
        "hashes": hashes,
        "hash": hashes.get("full_dhash") or legacy_hash,
        "rarity": data.rarity,
    }

    # Preserve an existing icon when editing a card; otherwise derive it from
    # the same scanned crop used to calculate hashes.
    record["icon"] = old.get("icon") or _save_permanent_icon(card_id, card_image)

    db[card_id] = record
    save_db(db)
    return {
        "status": "ok",
        "card_id": card_id,
        "card": record,
    }


@app.post("/admin/match-icon")
async def match_icon(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    image = await _read_image(file)
    card_image = _extract_single_card(image)
    hashes = calculate_card_hashes(card_image)
    db = load_db()
    card_id, score = _match_card(hashes, db)

    if card_id is None:
        return {
            "matched": False,
            "score": None,
            "hashes": hashes,
            "icon": "",
            "message": "Не удалось найти подходящую карту",
        }

    info = db[card_id]
    return {
        "matched": True,
        "card_id": card_id,
        "name": info.get("name", card_id),
        "rarity": info.get("rarity", 1),
        "score": round(score, 2),
        "hashes": hashes,
        "icon": info.get("icon", ""),
    }


@app.post("/admin/save-icon/{card_id}")
async def save_icon(
    card_id: str,
    file: UploadFile = File(...),
    manual: bool = False,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)

    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    image = await _read_image(file)
    card_image = _extract_single_card(image)

    # Always calculate hashes from the actual extracted card. Automatic
    # matching remains strict, but a manual admin confirmation is an explicit
    # identity decision and must also support high-quality viewer screenshots
    # whose pixels differ from the inventory screenshot.
    hashes = calculate_card_hashes(card_image)
    matched_id, score = _match_card(hashes, db)

    if not manual and (matched_id != card_id or score > ICON_MATCH_THRESHOLD):
        raise HTTPException(
            status_code=400,
            detail=f"Изображение не подтверждено для {card_id} (найдено: {matched_id}, distance={score:.2f}, порог={ICON_MATCH_THRESHOLD:g})",
        )

    icon = _prepare_icon(card_image)
    path = CARDS_DIR / f"{card_id}.webp"
    icon.save(path, "WEBP", quality=86, method=6)

    db[card_id]["icon"] = f"/static/cards/{path.name}"
    save_db(db)

    # Manual confirmation may intentionally have no automatic match.
    # Never return infinity to Starlette/JSON: JSON does not allow NaN/Infinity.
    safe_score = round(score, 2) if math.isfinite(score) else None

    return {
        "status": "ok",
        "card_id": card_id,
        "icon": db[card_id]["icon"],
        "score": safe_score,
    }


@app.post("/admin/save-icon-from-preview/{card_id}")
async def save_icon_from_preview(
    card_id: str,
    data: CardPreviewSchema,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    image = _load_admin_preview(data.preview)
    icon_url = _save_permanent_icon(card_id, image)
    db[card_id]["icon"] = icon_url
    save_db(db)
    return {"status": "ok", "card_id": card_id, "icon": icon_url}


@app.delete("/admin/delete-icon/{card_id}")
async def delete_icon(
    card_id: str,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    path = CARDS_DIR / f"{card_id}.webp"
    if path.exists():
        path.unlink()

    db[card_id].pop("icon", None)
    save_db(db)
    return {"status": "ok"}


@app.delete("/admin/delete-card/{card_id}")
async def delete_card(
    card_id: str,
    authorization: str | None = Header(default=None),
):
    require_admin(authorization)
    db = load_db()
    if card_id not in db:
        raise HTTPException(status_code=404, detail="Карта не найдена")

    path = CARDS_DIR / f"{card_id}.webp"
    if path.exists():
        path.unlink()

    del db[card_id]
    save_db(db)
    return {"status": "ok"}


def _github_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pubg-card-admin",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GitHub API: {exc.code} {detail[:500]}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API недоступен: {exc}") from exc


def _github_commit_branch() -> str:
    # Card data must never be pushed to main from the admin UI, even if an old
    # platform-specific environment variable still points there.
    return "feature/card-counter"


def _github_commit() -> dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend")
    branch = _github_commit_branch()

    if not token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN не задан на сервере")

    api = f"https://api.github.com/repos/{repo}"
    ref = _github_request("GET", f"{api}/git/ref/heads/{quote(branch, safe='')}", token)
    head_sha = ref["object"]["sha"]

    commit = _github_request("GET", f"{api}/git/commits/{head_sha}", token)
    base_tree = commit["tree"]["sha"]

    # Read the complete current tree so deleted card icons can be removed from
    # GitHub as well.
    tree_data = _github_request(
        "GET",
        f"{api}/git/trees/{head_sha}?recursive=1",
        token,
    )
    existing = {
        item["path"]: item["sha"]
        for item in tree_data.get("tree", [])
        if item.get("type") == "blob"
    }

    blobs = []
    card_json = DB_FILE.read_bytes()
    blobs.append(("card_hashes.json", card_json))

    local_icons = {
        str(p.relative_to(Path("."))).replace("\\", "/"): p
        for p in CARDS_DIR.glob("*.webp")
        if p.is_file()
    }

    for path, local_path in local_icons.items():
        blobs.append((path, local_path.read_bytes()))

    tree_entries: list[dict[str, Any]] = []

    for path, content in blobs:
        blob = _github_request(
            "POST",
            f"{api}/git/blobs",
            token,
            {
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    # Delete old card icons from GitHub when they no longer exist locally.
    for path in existing:
        if path.startswith("static/cards/") and path.endswith(".webp") and path not in local_icons:
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    new_tree = _github_request(
        "POST",
        f"{api}/git/trees",
        token,
        {"base_tree": base_tree, "tree": tree_entries},
    )

    new_commit = _github_request(
        "POST",
        f"{api}/git/commits",
        token,
        {
            "message": "Update PUBG card database and icons",
            "tree": new_tree["sha"],
            "parents": [head_sha],
        },
    )

    _github_request(
        "PATCH",
        f"{api}/git/refs/heads/{quote(branch, safe='')}",
        token,
        {"sha": new_commit["sha"], "force": False},
    )

    return {
        "status": "ok",
        "commit": new_commit["sha"],
        "url": new_commit.get("html_url", ""),
        "cards": len(load_db()),
        "icons": len(local_icons),
        "branch": branch,
    }



def _regenerate_hashes_from_icons() -> None:
    """Rebuild all card hashes from the saved card icons."""
    global HASH_REGEN_PROGRESS

    db = load_db()
    items = [
        (card_id, info)
        for card_id, info in db.items()
        if isinstance(info, dict) and (CARDS_DIR / f"{card_id}.webp").is_file()
    ]

    HASH_REGEN_PROGRESS = {
        "running": True,
        "current": 0,
        "total": len(items),
        "card_id": "",
        "status": "running",
        "updated": int(time.time()),
        "error": "",
    }

    try:
        for index, (card_id, info) in enumerate(items, start=1):
            icon_path = CARDS_DIR / f"{card_id}.webp"

            with Image.open(icon_path) as icon:
                hashes = calculate_card_hashes(icon.convert("RGB"))

            info["hashes"] = hashes
            info["hash"] = hashes.get("full_dhash", info.get("hash", ""))

            HASH_REGEN_PROGRESS.update({
                "current": index,
                "total": len(items),
                "card_id": card_id,
                "status": "running" if index < len(items) else "completed",
                "updated": int(time.time()),
            })

        save_db(db)

    except Exception as exc:
        HASH_REGEN_PROGRESS.update({
            "status": "error",
            "error": str(exc),
            "updated": int(time.time()),
        })
    finally:
        HASH_REGEN_PROGRESS["running"] = False


@app.post("/admin/regenerate-hashes")
async def regenerate_hashes(authorization: str | None = Header(default=None)):
    require_admin(authorization)

    if HASH_REGEN_PROGRESS.get("running"):
        return HASH_REGEN_PROGRESS

    # Start in a background thread so the admin page can poll progress.
    import threading
    thread = threading.Thread(
        target=_regenerate_hashes_from_icons,
        name="hash-regeneration",
        daemon=True,
    )
    thread.start()

    return {
        "running": True,
        "current": 0,
        "total": sum(
            1 for card_id, info in load_db().items()
            if isinstance(info, dict) and (CARDS_DIR / f"{card_id}.webp").is_file()
        ),
        "card_id": "",
        "status": "starting",
        "updated": int(time.time()),
        "error": "",
    }


@app.get("/admin/regenerate-hashes/progress")
async def regenerate_hashes_progress(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return HASH_REGEN_PROGRESS


@app.post("/admin/github-commit")
async def github_commit(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    return _github_commit()


register_trade_request_routes(
    app,
    _validate_telegram_init_data,
    get_or_create_user,
    get_trade_matches,
    _send_telegram_media_group,
    _card_icon_public_url,
)
