import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as model_app  # noqa: E402


def test_health_is_available_before_lazy_model_load():
    response = TestClient(model_app.app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_embedding_endpoint_uses_fake_loaded_service(monkeypatch):
    monkeypatch.setattr(
        model_app.service,
        "embed",
        lambda texts, dimensions=None: [[0.0] * (dimensions or 768) for _ in texts],
    )
    response = TestClient(model_app.app).post(
        "/v1/embeddings",
        json={
            "model": model_app.settings.embedding_model,
            "input": ["one", "two"],
            "dimensions": 32,
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 2
    assert len(response.json()["data"][0]["embedding"]) == 32


def test_rerank_endpoint_returns_ranked_results(monkeypatch):
    monkeypatch.setattr(
        model_app.service,
        "rerank",
        lambda query, documents, top_n: [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1},
        ][: top_n or len(documents)],
    )
    response = TestClient(model_app.app).post(
        "/v1/rerank",
        json={
            "model": model_app.settings.reranker_model,
            "query": "preferred language",
            "documents": ["Java note", "Python note"],
            "top_n": 2,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["index"] == 1


def test_model_server_auth_is_enforced_when_configured(monkeypatch):
    monkeypatch.setattr(model_app.settings, "api_key", "local-secret")
    response = TestClient(model_app.app).post(
        "/v1/embeddings",
        json={"model": model_app.settings.embedding_model, "input": "one"},
    )
    assert response.status_code == 401
