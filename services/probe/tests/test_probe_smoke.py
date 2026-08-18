"""Smoke tests: config parsing, bearer-token resolution, and basic endpoints."""

import main
from fastapi.testclient import TestClient

from tests.conftest import BEARER_TOKEN


class TestConfigParsing:
    def test_schedule_and_timeouts(self):
        assert main.INTERVAL_SECONDS == 60
        assert main.DEFAULT_TIMEOUT_SECONDS == 10

    def test_external_url(self):
        assert main.EXTERNAL_URL == "https://models.example.com"

    def test_targets(self):
        assert [t["modelId"] for t in main.TARGETS] == ["qwen3-8b", "qwen3-embed"]
        assert main.TARGETS[0]["runner"] == "chat"

    def test_bearer_token_resolved_from_key_map(self):
        # authBearerKey "probe" maps back to the token in the key-map file.
        assert main.BEARER_TOKEN == BEARER_TOKEN

    def test_external_headers_carry_bearer(self):
        headers = main._external_headers()
        assert headers["Authorization"] == f"Bearer {BEARER_TOKEN}"

    def test_internal_headers_have_no_auth(self):
        assert "Authorization" not in main._internal_headers()


class TestEndpoints:
    # TestClient without a context manager does not run the lifespan, so the
    # probe scheduler loop is not started during these tests.
    def test_healthz(self):
        r = TestClient(main.app).get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "interval_seconds": 60, "targets": 2}

    def test_metrics(self):
        r = TestClient(main.app).get("/metrics")
        assert r.status_code == 200
        assert "openserve_probe_runs_total" in r.text
