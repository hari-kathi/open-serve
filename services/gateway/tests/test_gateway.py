"""Gateway behavior tests: auth, skip-auth paths, public routes, model routing.

Backends are mocked with respx — no real upstream is contacted.
"""

import httpx
import main
import respx
from fastapi.testclient import TestClient

from tests.conftest import BACKEND_A, BACKEND_B, STATUS_URL, TEST_API_KEY, TEST_SOURCE

AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}


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

    def test_healthz_returns_200_when_key_map_loaded(self):
        # No respx mock: /healthz must not probe any backend.
        r = _client().get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestPublicRoutes:
    @respx.mock
    def test_status_prefix_forwards_without_auth(self):
        route = respx.get(f"{STATUS_URL}/status").mock(
            return_value=httpx.Response(200, text="<!DOCTYPE html><html>ok</html>")
        )
        r = _client().get("/status")
        assert r.status_code == 200
        assert route.called
        assert "ok" in r.text

    @respx.mock
    def test_static_prefix_forwards_without_auth(self):
        route = respx.get(f"{STATUS_URL}/static/style.css").mock(
            return_value=httpx.Response(200, text="body {}")
        )
        r = _client().get("/static/style.css")
        assert r.status_code == 200
        assert route.called


class TestModelRouting:
    @respx.mock
    def test_chat_routes_by_model(self):
        route = respx.post(f"{BACKEND_A}/v1/chat/completions").mock(
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
            headers=AUTH,
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "ping"}]},
        )
        assert r.status_code == 200
        assert route.called
        assert r.json()["choices"][0]["message"]["content"] == "pong"

    @respx.mock
    def test_chat_routes_second_model_to_other_backend(self):
        route = respx.post(f"{BACKEND_B}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        r = _client().post(
            "/v1/chat/completions", headers=AUTH, json={"model": "llama-3-70b"}
        )
        assert r.status_code == 200
        assert route.called

    @respx.mock
    def test_embeddings_route_by_model(self):
        route = respx.post(f"{BACKEND_B}/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"object": "list", "data": []})
        )
        r = _client().post(
            "/v1/embeddings", headers=AUTH, json={"model": "embed-small", "input": "hi"}
        )
        assert r.status_code == 200
        assert route.called

    @respx.mock
    def test_tokenize_routes_by_model(self):
        route = respx.post(f"{BACKEND_A}/tokenize").mock(
            return_value=httpx.Response(200, json={"tokens": [1, 2, 3]})
        )
        r = _client().post(
            "/tokenize", headers=AUTH, json={"model": "qwen3-8b", "prompt": "hi"}
        )
        assert r.status_code == 200
        assert route.called

    def test_missing_model_returns_400(self):
        r = _client().post(
            "/v1/chat/completions", headers=AUTH, json={"messages": []}
        )
        assert r.status_code == 400
        assert r.json() == {
            "error": {
                "message": "request body must include a 'model' field",
                "type": "invalid_request_error",
            }
        }

    def test_unparseable_body_returns_400(self):
        r = _client().post(
            "/v1/chat/completions",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b"not json",
        )
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"

    def test_unknown_model_returns_404(self):
        r = _client().post(
            "/v1/chat/completions", headers=AUTH, json={"model": "no-such-model"}
        )
        assert r.status_code == 404
        assert r.json() == {
            "error": {
                "message": "unknown model 'no-such-model'",
                "type": "invalid_request_error",
                "param": "model",
            }
        }

    @respx.mock
    def test_forwarded_request_strips_host_header(self):
        route = respx.post(f"{BACKEND_A}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        _client().post("/v1/chat/completions", headers=AUTH, json={"model": "qwen3-8b"})
        sent = route.calls.last.request
        # Host must be the backend's, not the incoming request's testserver host.
        assert sent.headers["host"] == "backend-a.test"
        assert sent.headers["authorization"] == f"Bearer {TEST_API_KEY}"


class TestModelsEndpoints:
    @respx.mock
    def test_models_listing_merges_and_dedupes_across_backends(self):
        respx.get(f"{BACKEND_A}/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={"object": "list", "data": [
                    {"id": "qwen3-8b", "object": "model"},
                    {"id": "shared-model", "object": "model"},
                ]},
            )
        )
        respx.get(f"{BACKEND_B}/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={"object": "list", "data": [
                    {"id": "llama-3-70b", "object": "model"},
                    {"id": "embed-small", "object": "model"},
                    {"id": "shared-model", "object": "model"},
                ]},
            )
        )
        r = _client().get("/v1/models", headers=AUTH)
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["data"]]
        assert ids == ["embed-small", "llama-3-70b", "qwen3-8b", "shared-model"]

    @respx.mock
    def test_models_listing_drops_failing_backend(self):
        respx.get(f"{BACKEND_A}/v1/models").mock(
            return_value=httpx.Response(200, json={"object": "list", "data": [{"id": "qwen3-8b"}]})
        )
        respx.get(f"{BACKEND_B}/v1/models").mock(side_effect=httpx.ConnectError("down"))
        r = _client().get("/v1/models", headers=AUTH)
        assert r.status_code == 200
        assert [m["id"] for m in r.json()["data"]] == ["qwen3-8b"]

    @respx.mock
    def test_get_model_by_id_routes_by_path(self):
        route = respx.get(f"{BACKEND_B}/v1/models/llama-3-70b").mock(
            return_value=httpx.Response(200, json={"id": "llama-3-70b", "object": "model"})
        )
        r = _client().get("/v1/models/llama-3-70b", headers=AUTH)
        assert r.status_code == 200
        assert route.called
        assert r.json()["id"] == "llama-3-70b"

    @respx.mock
    def test_delete_model_by_id_routes_by_path(self):
        route = respx.delete(f"{BACKEND_A}/v1/models/qwen3-8b").mock(
            return_value=httpx.Response(200, json={"id": "qwen3-8b", "deleted": True})
        )
        r = _client().delete("/v1/models/qwen3-8b", headers=AUTH)
        assert r.status_code == 200
        assert route.called

    def test_model_id_unknown_in_path_returns_404(self):
        r = _client().get("/v1/models/no-such-model", headers=AUTH)
        assert r.status_code == 404
        assert r.json()["error"]["param"] == "model"


class TestMetricsLabels:
    @respx.mock
    def test_org_header_labels_metrics(self):
        respx.post(f"{BACKEND_A}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
            )
        )
        client = _client()
        r = client.post(
            "/v1/chat/completions",
            headers={**AUTH, "x-openserve-org-id": "acme"},
            json={"model": "qwen3-8b"},
        )
        assert r.status_code == 200
        metrics = client.get("/metrics").text
        assert f'openserve_requests_total{{model="qwen3-8b",org="acme",source="{TEST_SOURCE}",tier="unknown"}}' in metrics

    def test_routing_rejections_counted_in_error_metric(self):
        client = _client()
        client.post("/v1/chat/completions", headers=AUTH, json={"messages": []})
        client.post("/v1/chat/completions", headers=AUTH, json={"model": "nope"})
        metrics = client.get("/metrics").text
        assert 'error_type="missing_model"' in metrics
        assert 'error_type="unknown_model"' in metrics


class TestStreaming:
    @respx.mock
    def test_streaming_passthrough(self):
        chunks = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        respx.post(f"{BACKEND_A}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, content=chunks, headers={"content-type": "text/event-stream"}
            )
        )
        with _client().stream(
            "POST",
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "qwen3-8b", "stream": True},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            body = b"".join(r.iter_bytes())
        assert body == chunks
