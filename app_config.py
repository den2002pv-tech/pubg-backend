import os
from pathlib import Path
import subprocess


DEFAULT_PUBLIC_BASE_URL = "https://pubg-card.enzonic.dev"


def get_public_base_url() -> str:
    configured = os.getenv("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip().rstrip("/")
    return configured or DEFAULT_PUBLIC_BASE_URL


def get_app_commit() -> str:
    for variable in ("APP_COMMIT", "GITHUB_SHA", "RENDER_GIT_COMMIT", "SOURCE_VERSION"):
        commit = os.getenv(variable, "").strip()
        if commit:
            return commit
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"
