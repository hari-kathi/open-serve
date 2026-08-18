"""Gateway behavior tests: auth, skip-auth paths, public forwards, forwarding.

The backend is mocked with respx — no real upstream is contacted.
"""

import httpx
import main
import respx
from fastapi.testclient import TestClient

from tests.conftest import BACKEND_URL, TEST_API_KEY, TEST_SOURCE


def _client() -> TestClient:
    return TestClient(main.app)


class TestAuth:
    def test_missing_authorization_returns_401(self):
        r = _client().post("/v1/chat/completions", json={"model": "qwen3-8b"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Missing Bearer token"

    def test_non_bearer_authorization_returns_401(self):
        r = _client().post(
            "/v1/chat/completions",
            headers={"Authorization": "Basic abc"},
            json={"model": "qwen3-8b"},
        )
        assert r.status_code == 401

    def test_unknown_key_returns_401(self):
        r = _client().post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer not-a-real-key"},
            json={"model": "qwen3-8b"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid API key"

    @respx.mock
    def test_valid_key_from_key_map_file_is_forwarded(self):
        route = respx.post(f"{BACKEND_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
            )
        )
        r = _client().post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "ping"}]},
        )
        assert r.status_code == 200
        assert route.called
        assert r.json()["choices"][0]["message"]["content"] == "pong"

    @respx.mock
    def test_forwarded_request_strips_host_header(self):
        route = respx.post(f"{BACKEND_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        _client().post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={"model": "qwen3-8b"},
        )
        sent = route.calls.last.request
        # Host must be the backend's, not the incoming request's testserver host.
        assert sent.headers["host"] == "backend.test"
        assert sent.headers["authorization"] == f"Bearer {TEST_API_KEY}"


class TestSkipAuthPaths:
    def test_health_needs_no_auth(self):
        r = _client().get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_root_needs_no_auth(self):
        r = _client().get("/")
        assert r.status_code == 200

    def test_metrics_needs_no_auth(self):
        r = _client().get("/metrics")
        assert r.status_code == 200
        assert "openserve_requests_total" in r.text

    @respx.mock
    def test_healthz_needs_no_auth_and_checks_backend(self):
        respx.get(f"{BACKEND_URL}/v1/models").mock(
            return_value=httpx.Response(200, json={"object": "list", "data": []})
        )
        r = _client().get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "backend": "ok"}


class TestPublicForwardPrefixes:
    @respx.mock
    def test_status_prefix_forwards_without_auth(self):
        route = respx.get(f"{BACKEND_URL}/status").mock(
            return_value=httpx.Response(200, text="<!DOCTYPE html><html>ok</html>")
        )
        r = _client().get("/status")
        assert r.status_code == 200
        assert route.called
        assert "ok" in r.text

    @respx.mock
    def test_static_prefix_forwards_without_auth(self):
        route = respx.get(f"{BACKEND_URL}/static/style.css").mock(
            return_value=httpx.Response(200, text="body {}")
        )
        r = _client().get("/static/style.css")
        assert r.status_code == 200
        assert route.called

    def test_default_prefixes_from_env_parsing(self):
        assert main.PUBLIC_FORWARD_PREFIXES == ("/status", "/static/")

    def test_default_org_header_name(self):
        assert main.ORG_ID_HEADER == "x-openserve-org-id"

    @respx.mock
    def test_org_header_labels_metrics(self):
        respx.post(f"{BACKEND_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
            )
        )
        client = _client()
        r = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {TEST_API_KEY}",
                "x-openserve-org-id": "acme",
            },
            json={"model": "qwen3-8b"},
        )
        assert r.status_code == 200
        metrics = client.get("/metrics").text
        assert f'openserve_requests_total{{model="qwen3-8b",org="acme",source="{TEST_SOURCE}",tier="unknown"}}' in metrics
