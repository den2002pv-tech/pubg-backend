import json
import os
from pathlib import Path
import subprocess
from urllib.request import Request as UrlRequest, urlopen


DEFAULT_PUBLIC_BASE_URL = "https://pubg-card.enzonic.dev"


def get_public_base_url() -> str:
    configured = os.getenv("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip().rstrip("/")
    return configured or DEFAULT_PUBLIC_BASE_URL


def _github_branch() -> str:
    for variable in ("GITHUB_BRANCH", "BRANCH", "COUNTER_TEST_BRANCH"):
        value = os.getenv(variable, "").strip()
        if value:
            return value
    return "feature/card-counter"


def _github_head_sha(timeout: float = 3.0) -> str:
    repo = os.getenv("GITHUB_REPO", "den2002pv-tech/pubg-backend").strip() or "den2002pv-tech/pubg-backend"
    branch = _github_branch()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pubg-card-app-config",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(UrlRequest(url, headers=headers), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    sha = str(payload.get("sha", "") or "").strip()
    return sha[:12] if sha else ""


def get_app_commit() -> str:
    for variable in ("APP_COMMIT", "GITHUB_SHA", "RENDER_GIT_COMMIT", "SOURCE_VERSION"):
        commit = os.getenv(variable, "").strip()
        if commit:
            return commit
    # Штатная автосинхронизация Enzonic копирует файлы без обновления локального .git,
    # поэтому git rev-parse здесь врёт (залипает на старом SHA). Спрашиваем актуальный
    # HEAD ветки напрямую у GitHub — это и есть версия задеплоенного кода.
    github_sha = _github_head_sha()
    if github_sha:
        return github_sha
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
