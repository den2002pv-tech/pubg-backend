import os


DEFAULT_PUBLIC_BASE_URL = "https://pubg-card.enzonic.dev"


def get_public_base_url() -> str:
    configured = os.getenv("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip().rstrip("/")
    return configured or DEFAULT_PUBLIC_BASE_URL


def get_app_commit() -> str:
    return os.getenv("APP_COMMIT", "unknown").strip() or "unknown"
