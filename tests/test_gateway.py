import json
import sys
from pathlib import Path
import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
from app import create_app

CONFIG = {"upstream": "http://model:8001", "upstream_key": "backend-test-only",
          "users": {"alice": "alice-test-only", "bob": "bob-test-only"},
          "global_concurrency": 2, "per_user_concurrency": 1}
AUTH = {"Authorization": "Bearer alice-test-only"}


def test_auth_and_endpoint_restrictions():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": []})
    with TestClient(create_app(CONFIG, httpx.MockTransport(handler))) as c:
        assert c.get("/v1/models").status_code == 401
        assert c.get("/v1/models", headers={"Authorization": "Bearer unknown"}).status_code == 401
        assert c.get("/v1/admin", headers=AUTH).status_code == 404
        assert calls == []
        assert c.get("/v1/models", headers=AUTH).status_code == 200
        assert calls[0].headers["authorization"] == "Bearer backend-test-only"
        assert "alice-test-only" not in str(calls[0].headers)


def test_stream_preserves_bytes_status_and_releases_slot():
    body = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'
    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    app = create_app(CONFIG, httpx.MockTransport(handler))
    with TestClient(app) as c:
        for _ in range(3):
            r = c.post("/v1/chat/completions", headers=AUTH, json={"stream": True})
            assert r.content == body
            assert r.headers["content-type"] == "text/event-stream"
            assert app.state.total == 0


def test_failures_and_quota_release():
    def handler(request):
        raise httpx.ConnectError("offline", request=request)
    app = create_app(CONFIG, httpx.MockTransport(handler))
    with TestClient(app) as c:
        assert c.get("/v1/models", headers=AUTH).status_code == 502
        assert app.state.total == 0
        assert c.get("/readyz").status_code == 503
        app.state.active["alice"] = 1
        r = c.get("/v1/models", headers=AUTH)
        assert r.status_code == 429
        assert r.headers["retry-after"] == "2"


def test_oversized_and_invalid_body_do_not_call_model():
    def handler(request):
        raise AssertionError("model must not be called")
    with TestClient(create_app(CONFIG, httpx.MockTransport(handler))) as c:
        assert c.post("/v1/chat/completions", headers=AUTH, content=b"invalid").status_code == 400
        assert c.post("/v1/chat/completions", headers=AUTH, json=[]).status_code == 400
        assert c.post("/v1/chat/completions", headers=AUTH,
                      content=b"x" * (8 * 1024 * 1024 + 1)).status_code == 413

