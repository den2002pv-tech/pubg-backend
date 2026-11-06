import base64
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import main


class GithubAPI:
    def __init__(self, files, *, truncated=False):
        self.files = files
        self.truncated = truncated
        self.calls = []

    def __call__(self, method, url, token, payload=None):
        self.calls.append((method, url, payload))
        if "/git/ref/heads/" in url:
            assert url.endswith("feature%2Fcard-counter")
            return {"object": {"sha": "head"}}
        if url.endswith("/git/commits/head"):
            return {"tree": {"sha": "base"}}
        if url.endswith("/git/trees/head?recursive=1"):
            return {"tree": [{"path": path, "type": "blob", "sha": main._git_blob_sha(content)}
                             for path, content in self.files.items()], "truncated": self.truncated}
        if url.endswith("/git/blobs"):
            content = base64.b64decode(payload["content"])
            return {"sha": main._git_blob_sha(content)}
        if url.endswith("/git/trees"):
            return {"sha": "new-tree"}
        if url.endswith("/git/commits"):
            return {"sha": "new-commit", "html_url": "https://example.test/commit"}
        if "/git/refs/heads/" in url:
            assert url.endswith("feature%2Fcard-counter")
            return {}
        raise AssertionError((method, url))


@pytest.fixture
def setup_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cards = tmp_path / "static/cards"
    cards.mkdir(parents=True)
    monkeypatch.setattr(main, "CARDS_DIR", Path("static/cards"))
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "card_hashes.json")
    monkeypatch.setattr(main, "load_db", lambda: {"one": {}})
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_BRANCH", "main")

    def prepare(local, remote, *, truncated=False):
        for path, content in local.items():
            destination = tmp_path / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        api = GithubAPI(remote, truncated=truncated)
        monkeypatch.setattr(main, "_github_request", api)
        return api

    return prepare


@pytest.mark.parametrize("content", [b"", b"hello\n", bytes(range(256))])
def test_git_blob_sha_matches_git(content):
    expected = subprocess.run(["git", "hash-object", "--stdin"], input=content,
                              capture_output=True, check=True).stdout.decode().strip()
    assert main._git_blob_sha(content) == expected


def test_commit_uploads_only_new_and_changed_files_and_deletes_missing_icons(setup_commit):
    local = {"card_hashes.json": b"new catalog", "static/cards/same.webp": b"same",
             "static/cards/changed.webp": b"new", "static/cards/new.webp": b"added"}
    remote = {"card_hashes.json": b"old catalog", "static/cards/same.webp": b"same",
              "static/cards/changed.webp": b"old", "static/cards/deleted.webp": b"gone",
              "unrelated.txt": b"keep"}
    api = setup_commit(local, remote)
    result = main._github_commit()

    uploads = [base64.b64decode(payload["content"]) for method, url, payload in api.calls
               if method == "POST" and url.endswith("/git/blobs")]
    assert sorted(uploads) == sorted([b"new catalog", b"new", b"added"])
    tree = next(payload for method, url, payload in api.calls if method == "POST" and url.endswith("/git/trees"))
    entries = {entry["path"]: entry for entry in tree["tree"]}
    assert tree["base_tree"] == "base"
    assert set(entries) == {"card_hashes.json", "static/cards/changed.webp", "static/cards/new.webp", "static/cards/deleted.webp"}
    assert entries["static/cards/deleted.webp"] == {"path": "static/cards/deleted.webp", "mode": "100644", "type": "blob", "sha": None}
    assert entries["static/cards/new.webp"]["sha"] == main._git_blob_sha(b"added")
    commit_calls = [payload for method, url, payload in api.calls if method == "POST" and url.endswith("/git/commits")]
    assert len(commit_calls) == 1 and commit_calls[0]["parents"] == ["head"]
    assert api.calls[-1][2] == {"sha": "new-commit", "force": False}
    assert result == {"status": "ok", "commit": "new-commit", "url": "https://example.test/commit", "cards": 1, "icons": 3, "branch": "feature/card-counter"}


def test_no_changes_skips_all_writes(setup_commit):
    files = {"card_hashes.json": b"catalog", "static/cards/same.webp": b"same"}
    api = setup_commit(files, files)
    result = main._github_commit()
    assert result["status"] == "no_changes" and result["commit"] == "head"
    assert [method for method, _, _ in api.calls] == ["GET"] * 3


def test_deletion_alone_creates_commit_without_blob_upload(setup_commit):
    local = {"card_hashes.json": b"catalog"}
    api = setup_commit(local, {**local, "static/cards/old.webp": b"old"})
    assert main._github_commit()["status"] == "ok"
    assert not any(url.endswith("/git/blobs") for _, url, _ in api.calls)
    tree = next(payload for method, url, payload in api.calls if method == "POST" and url.endswith("/git/trees"))
    assert tree["tree"] == [{"path": "static/cards/old.webp", "mode": "100644", "type": "blob", "sha": None}]


def test_incomplete_tree_never_writes(setup_commit):
    api = setup_commit({"card_hashes.json": b"catalog"}, {}, truncated=True)
    with pytest.raises(main.HTTPException) as exc:
        main._github_commit()
    assert exc.value.status_code == 502
    assert all(method == "GET" for method, _, _ in api.calls)


def test_upload_failure_does_not_create_commit(setup_commit, monkeypatch):
    api = setup_commit({"card_hashes.json": b"new"}, {"card_hashes.json": b"old"})
    def failing_request(method, url, token, payload=None):
        if url.endswith("/git/blobs"):
            raise main.HTTPException(status_code=502, detail="GitHub unavailable")
        return api(method, url, token, payload)
    monkeypatch.setattr(main, "_github_request", failing_request)
    with pytest.raises(main.HTTPException) as exc:
        main._github_commit()
    assert exc.value.status_code == 502
    assert all(method == "GET" for method, _, _ in api.calls)


def test_missing_token_fails_before_request(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(main.HTTPException) as exc:
        main._github_commit()
    assert exc.value.status_code == 503


@pytest.mark.parametrize("error", [HTTPError("https://example.test", 403, "forbidden", {}, None), URLError(TimeoutError("timed out"))])
def test_github_request_preserves_api_error_handling(monkeypatch, error):
    def fail(request, timeout):
        assert timeout == 30
        raise error
    if isinstance(error, HTTPError):
        from io import BytesIO
        error.fp = BytesIO(b"forbidden")
    monkeypatch.setattr(main, "urlopen", fail)
    with pytest.raises(main.HTTPException) as exc:
        main._github_request("GET", "https://example.test", "test-token")
    assert exc.value.status_code == 502
