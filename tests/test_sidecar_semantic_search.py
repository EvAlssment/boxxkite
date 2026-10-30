"""Security and behavior tests for the sandbox-local semantic-search MVP."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

import main as sidecar_main
import sidecar_semantic_search


def _setup_workspace(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    workspace = tmp_path / "workspace"
    outputs = tmp_path / "outputs"
    uploads = tmp_path / "uploads"
    skills = tmp_path / "skills"
    scratch = tmp_path / "tmp"
    for path in (workspace, outputs, uploads, skills, scratch):
        path.mkdir()
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(workspace))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(outputs))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(uploads))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(skills))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(scratch))
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", os.getuid())
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", os.getgid())
    sidecar_semantic_search.reset_semantic_search_index()
    return workspace


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _headers() -> dict[str, str]:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def test_semantic_search_ranks_relevant_file_lines_deterministically(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    (workspace / "database.py").write_text("def connect_database():\n    return True\n")
    (workspace / "retries.py").write_text(
        "def retry_request():\n    # retry transient failures\n    return retry_request()\n"
    )

    response = _client().post(
        "/semantic-search",
        json={"query": "retry logic", "max_results": 5},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["matches"][0]["path"].endswith("/retries.py")
    assert body["matches"][0]["line"] in {1, 2, 3}
    assert body["indexed_files"] == 2
    assert body["notes"][0].startswith("Deterministic lexical ranking")


def test_file_create_and_str_replace_invalidate_cached_documents(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    path = workspace / "handler.py"
    path.write_text("def retry_handler():\n    return 'cache lookup'\n")
    client = _client()

    first = client.post(
        "/semantic-search", json={"query": "cache lookup"}, headers=_headers()
    )
    assert first.status_code == 200
    assert first.json()["matches"]

    replaced = client.post(
        "/file-create",
        json={"path": "handler.py", "content": "def retry_handler():\n    return 'retry'\n"},
        headers=_headers(),
    )
    assert replaced.status_code == 200

    after_create = client.post(
        "/semantic-search", json={"query": "cache lookup"}, headers=_headers()
    )
    assert after_create.status_code == 200
    assert after_create.json()["matches"] == []

    edited = client.post(
        "/str-replace",
        json={
            "path": "handler.py",
            "old_str": "'retry'",
            "new_str": "'timeout'",
        },
        headers=_headers(),
    )
    assert edited.status_code == 200

    after_replace = client.post(
        "/semantic-search", json={"query": "timeout handler"}, headers=_headers()
    )
    assert after_replace.status_code == 200
    assert after_replace.json()["matches"]
    assert any("timeout" in match["text"] for match in after_replace.json()["matches"])


def test_semantic_search_skips_symlinks_that_escape_allowed_roots(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("AWS_SECRET_ACCESS_KEY=must-not-leak")
    os.symlink(outside, workspace / "secret-link.txt")

    response = _client().post(
        "/semantic-search",
        json={"query": "AWS secret access key"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["matches"] == []
    assert "must-not-leak" not in response.text


def test_semantic_search_does_not_follow_symlink_swapped_in_before_open(
    tmp_path, monkeypatch
):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    candidate = workspace / "secret.txt"
    candidate.write_text("ordinary workspace content")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("AWS_SECRET_ACCESS_KEY=must-not-leak")
    real_open = os.open
    swapped = False

    def swap_before_file_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == candidate.name and kwargs.get("dir_fd") is not None and not swapped:
            candidate.unlink()
            os.symlink(outside, candidate)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sidecar_semantic_search.os, "open", swap_before_file_open)
    response = _client().post(
        "/semantic-search",
        json={"query": "AWS secret access key"},
        headers=_headers(),
    )

    assert swapped
    assert response.status_code == 200
    assert response.json()["matches"] == []
    assert "must-not-leak" not in response.text


def test_semantic_search_skips_fifo_without_blocking(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    os.mkfifo(workspace / "waiting.fifo")

    response = _client().post(
        "/semantic-search",
        json={"query": "waiting fifo"},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["matches"] == []
    assert response.json()["indexed_files"] == 0


def test_semantic_search_enforces_index_size_bounds(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    (workspace / "a.py").write_text("retry handler\n")
    (workspace / "b.py").write_text("retry handler\n")
    monkeypatch.setattr(sidecar_semantic_search, "MAX_INDEXED_FILES", 1)

    response = _client().post(
        "/semantic-search",
        json={"query": "retry handler", "max_results": 50},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["indexed_files"] == 1
    assert body["truncated"] is True
    assert len(body["matches"]) == 1


def test_semantic_search_retains_only_top_ranked_results(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    (workspace / "many.py").write_text("".join("needle\n" for _ in range(5000)))

    response = _client().post(
        "/semantic-search",
        json={"query": "needle", "max_results": 1},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["line"] == 1
    assert body["truncated"] is True


def test_semantic_search_index_can_be_cleared_for_session_recycling(tmp_path, monkeypatch):
    workspace = _setup_workspace(tmp_path, monkeypatch)
    (workspace / "note.py").write_text("tenant one private marker\n")
    client = _client()
    assert client.post(
        "/semantic-search", json={"query": "private marker"}, headers=_headers()
    ).json()["matches"]

    sidecar_main.reset_semantic_search_index()

    assert sidecar_semantic_search._INDEX._documents == {}
