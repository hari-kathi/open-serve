"""Layer 2 — Snapshotter driven against a Starlette-based mock Prometheus.

Verifies the v2 contract: probe-interval hourly-strip aggregation, ≥2-consecutive
incident threshold, stale-snapshot fallback, model/endpoint data model.
"""

import json
import socket
import threading
import time
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from main import PrometheusClient, Snapshotter, State


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MockPromHandler(BaseHTTPRequestHandler):
    """Routes by (path, substring-of-query) so the three distinct queries
    Snapshotter issues each get their own canned response.
    """

    responses: dict[str, list[tuple[str | None, list[dict]]]] = {}
    status_override: int | None = None
    call_count: dict[str, int] = defaultdict(int)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        query_text = (params.get("query") or [""])[0]
        _MockPromHandler.call_count[path] += 1

        if _MockPromHandler.status_override is not None:
            self.send_response(_MockPromHandler.status_override)
            self.end_headers()
            self.wfile.write(b'{"status":"error"}')
            return

        result: list[dict] = []
        for needle, canned in _MockPromHandler.responses.get(path, []):
            if needle is None or needle in query_text:
                result = canned
                break

        payload = {
            "status": "success",
            "data": {
                "resultType": "matrix" if "_range" in path else "vector",
                "result": result,
            },
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kwargs):
        pass


@pytest.fixture
def mock_prom():
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _MockPromHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _MockPromHandler.responses = {}
    _MockPromHandler.status_override = None
    _MockPromHandler.call_count = defaultdict(int)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _vector(model: str, endpoint: str, value: float) -> dict:
    return {
        "metric": {"model": model, "endpoint": endpoint, "tier": "production"},
        "value": [0, str(value)],
    }


def _matrix(model: str, endpoint: str, samples: list[tuple[float, float]]) -> dict:
    return {
        "metric": {"model": model, "endpoint": endpoint, "tier": "production"},
        "values": [[ts, str(v)] for ts, v in samples],
    }


def _setup(
    *,
    success: list[dict],
    latency: list[dict],
    range_data: list[dict] | None = None,
    errors_data: list[dict] | None = None,
    latency_range_data: list[dict] | None = None,
) -> dict:
    """Build the mock response table for the queries the Snapshotter issues.

    Needles are matched as substrings of the PromQL, first match wins; they are
    distinct per query (probe_errors_total / probe_latency_seconds_bucket /
    openserve_probe_success) so order doesn't cause cross-matches.
    """
    return {
        "/api/v1/query": [
            ("openserve_probe_success", success),
            ("probe_latency_seconds", latency),
        ],
        "/api/v1/query_range": [
            ("probe_errors_total", errors_data or []),
            ("probe_latency_seconds_bucket", latency_range_data or []),
            ("openserve_probe_success", range_data or []),
        ],
    }


pytestmark = pytest.mark.asyncio


class TestSnapshotterAgainstMock:
    async def test_healthy_path(self, mock_prom):
        # Generate 30 days of healthy probe samples at 15-min cadence.
        now = time.time()
        step = 900
        samples = [(now - (30 * 86400) + i * step, 1.0) for i in range((30 * 86400) // step)]
        _MockPromHandler.responses = _setup(
            success=[_vector("qwen3-8b", "external", 1.0)],
            latency=[_vector("qwen3-8b", "external", 2.0)],
            range_data=[_matrix("qwen3-8b", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom,
                model_metadata={"qwen3-8b": {"category": "Model"}},
                slo_per_model={"qwen3-8b": 5.0},
            ).get()
        finally:
            await prom.close()

        assert snap.fallback_mode is False
        assert "Model" in snap.models_by_category
        models = snap.models_by_category["Model"]
        assert len(models) == 1
        assert models[0].state == State.HEALTHY
        assert snap.banner_state == State.HEALTHY
        assert len(snap.recent_history) == 30  # one entry per day

    async def test_cache_holds_within_ttl(self, mock_prom):
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 1.0)],
        )
        prom = PrometheusClient(mock_prom)
        snapper = Snapshotter(prom, {"m1": {"category": "Model"}}, {"m1": 5.0}, cache_ttl_s=300)
        try:
            await snapper.get()
            first = sum(_MockPromHandler.call_count.values())
            await snapper.get()
            await snapper.get()
            second = sum(_MockPromHandler.call_count.values())
        finally:
            await prom.close()
        assert second == first

    async def test_serves_stale_cache_when_prom_briefly_down(self, mock_prom):
        # First build succeeds.
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 1.0)],
        )
        prom = PrometheusClient(mock_prom)
        snapper = Snapshotter(prom, {"m1": {"category": "Model"}}, {"m1": 5.0}, cache_ttl_s=0)
        try:
            fresh = await snapper.get()
            assert fresh.data_stale_s == 0.0
            assert fresh.fallback_mode is False
            # Simulate Prom outage on next call.
            _MockPromHandler.status_override = 503
            stale = await snapper.get()
        finally:
            await prom.close()
        assert stale.fallback_mode is False, "should serve stale, not fallback"
        assert stale.data_stale_s > 0, "data_stale_s should track snapshot age"
        assert stale.banner_state == State.HEALTHY, "stale snapshot keeps prior banner"

    async def test_cold_cache_fallback_when_prom_down(self, mock_prom):
        _MockPromHandler.status_override = 503
        prom = PrometheusClient(mock_prom, timeout_s=1.0)
        try:
            snap = await Snapshotter(prom, {}, {}).get()
        finally:
            await prom.close()
        assert snap.fallback_mode is True
        assert snap.banner_state == State.UNKNOWN

    async def test_degraded_when_latency_above_slo(self, mock_prom):
        _MockPromHandler.responses = _setup(
            success=[_vector("slow", "external", 1.0)],
            latency=[_vector("slow", "external", 12.0)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(prom, {"slow": {"category": "Model"}}, {"slow": 5.0}).get()
        finally:
            await prom.close()
        assert snap.banner_state == State.DEGRADED

    async def test_down_when_success_rate_low(self, mock_prom):
        _MockPromHandler.responses = _setup(
            success=[_vector("broken", "external", 0.1)],
            latency=[_vector("broken", "external", 2.0)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(prom, {"broken": {"category": "Model"}}, {"broken": 5.0}).get()
        finally:
            await prom.close()
        assert snap.banner_state == State.DOWN

    async def test_single_blip_does_not_count_as_incident(self, mock_prom):
        """A single non-healthy sample (e.g., KubeRay rolling swap) should be filtered."""
        now = time.time()
        step = 900
        # 30d of healthy except ONE failed sample 1d ago.
        samples = []
        n = (30 * 86400) // step
        for i in range(n):
            ts = now - (30 * 86400) + i * step
            samples.append((ts, 1.0 if i != (n - 96) else 0.0))  # one blip 1 day ago
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0},
                incident_min_consecutive_samples=2,
            ).get()
        finally:
            await prom.close()
        # No incident in history — the single blip should be filtered.
        flat = [i for day in snap.recent_history for i in day.incidents]
        assert flat == [], f"single blip should not produce incidents, got {len(flat)}"

    async def test_two_consecutive_failures_count_as_incident(self, mock_prom):
        now = time.time()
        step = 900
        samples = []
        n = (30 * 86400) // step
        bad_indices = {n - 96, n - 95}  # two consecutive failures ~1 day ago
        for i in range(n):
            ts = now - (30 * 86400) + i * step
            samples.append((ts, 0.0 if i in bad_indices else 1.0))
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0},
                incident_min_consecutive_samples=2,
            ).get()
        finally:
            await prom.close()
        flat = [i for day in snap.recent_history for i in day.incidents]
        assert len(flat) == 1
        assert flat[0].peak_state == State.DOWN

    async def test_hourly_strip_aggregates_partial_outage(self, mock_prom):
        """An hour with a sustained sub-100% success rate renders as a
        non-healthy bar with <100% uptime.

        The probe grid is aligned to the UTC hour so the two consecutive
        failures land deterministically in the same hour bucket (4 samples per
        hour at 15-min cadence) regardless of when the test runs.
        """
        from datetime import datetime, timezone
        now = time.time()
        step = 900
        # Hour-align the sample grid: 4 samples per hour at :00/:15/:30/:45.
        start = (
            datetime.fromtimestamp(now - (14 * 86400), tz=timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
            .timestamp()
        )
        n = int((now - start) // step)
        # Two consecutive failures (=30 min) inside one hour ~2 days back.
        # idx % 4 == 0 → idx (:00) and idx+1 (:15) share the same hour bucket.
        idx = (2 * 24) * 4
        bad = {idx, idx + 1}
        samples = [(start + i * step, 0.0 if i in bad else 1.0) for i in range(n)]
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(prom, {"m1": {"category": "Model"}}, {"m1": 5.0}).get()
        finally:
            await prom.close()
        strip = snap.models_by_category["Model"][0].hourly_strip
        affected = [c for c in strip if c.uptime_fraction is not None and c.uptime_fraction < 1.0]
        assert affected, "expected one hour with sub-100% uptime"
        # Two consecutive failures → DOWN (or DEGRADED), never HEALTHY.
        assert all(c.state in (State.DEGRADED, State.DOWN) for c in affected)
        assert affected[0].uptime_fraction == pytest.approx(0.5)

    async def test_no_data_hours_show_unknown(self, mock_prom):
        """If Prometheus has no samples for older hours, those bars are UNKNOWN."""
        now = time.time()
        step = 900
        strip_days = 14
        # Only last 3 days of data; older hours in the window have no samples.
        samples = [(now - i * step, 1.0) for i in range(3 * 96)]
        samples.reverse()
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0}, strip_days=strip_days
            ).get()
        finally:
            await prom.close()
        strip = snap.models_by_category["Model"][0].hourly_strip
        assert len(strip) == strip_days * 24
        # Oldest bars (no data this far back) unknown; most recent hour healthy.
        assert strip[0].state == State.UNKNOWN
        assert strip[-1].state == State.HEALTHY

    async def test_category_order_respects_configured_order(self, mock_prom):
        """The configured category order controls section ordering on the page.

        Important for multi-model-type deployments: as operators add rerankers,
        OCR, audio, etc., the page should keep a predictable section order.
        """
        _MockPromHandler.responses = {
            "/api/v1/query": [
                ("openserve_probe_success", [
                    _vector("a-emb", "external", 1.0),
                    _vector("b-llm", "external", 1.0),
                    _vector("c-rerank", "external", 1.0),
                ]),
                ("probe_latency_seconds", []),
            ],
            "/api/v1/query_range": [(None, [])],
        }
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom,
                model_metadata={
                    "a-emb": {"category": "Embedding"},
                    "b-llm": {"category": "LLM"},
                    "c-rerank": {"category": "Reranker"},
                },
                slo_per_model={},
                category_order=["LLM", "Embedding", "Reranker"],
            ).get()
        finally:
            await prom.close()
        assert list(snap.models_by_category.keys()) == ["LLM", "Embedding", "Reranker"]

    async def test_unknown_categories_sort_alphabetically_after_known(self, mock_prom):
        _MockPromHandler.responses = {
            "/api/v1/query": [
                ("openserve_probe_success", [
                    _vector("a", "external", 1.0),
                    _vector("b", "external", 1.0),
                    _vector("c", "external", 1.0),
                ]),
                ("probe_latency_seconds", []),
            ],
            "/api/v1/query_range": [(None, [])],
        }
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom,
                model_metadata={
                    "a": {"category": "Zeta"},
                    "b": {"category": "LLM"},
                    "c": {"category": "Alpha"},
                },
                slo_per_model={},
                category_order=["LLM"],  # only LLM is "known"; others fall back to alpha
            ).get()
        finally:
            await prom.close()
        keys = list(snap.models_by_category.keys())
        assert keys[0] == "LLM"
        # Alpha < Zeta alphabetically
        assert keys[1:] == ["Alpha", "Zeta"]

    async def test_rolling_update_blip_keeps_hour_green(self, mock_prom):
        """A single failed probe sample in an hour (typical of a KubeRay
        zero-downtime swap catching one probe cycle) should NOT flip the
        hourly bar to DEGRADED. Matches the >=2-consecutive-samples threshold
        the incident detector uses, so the strip and recent-history agree.
        """
        now = time.time()
        step = 900
        # 30d of healthy probes EXCEPT a single failed sample ~5 days ago.
        n = (30 * 86400) // step
        bad_idx = n - (5 * 96 + 48)  # ~5 days ago, around midday
        samples = [
            (now - (30 * 86400) + i * step, 0.0 if i == bad_idx else 1.0)
            for i in range(n)
        ]
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0},
                incident_min_consecutive_samples=2,
            ).get()
        finally:
            await prom.close()
        ep = snap.models_by_category["Model"][0].endpoints[0]
        # Find the hour with the single failed sample.
        blip_hour = next(
            (c for c in ep.hourly_strip if c.uptime_fraction is not None and c.uptime_fraction < 1.0),
            None,
        )
        assert blip_hour is not None, "expected one hour with sub-100% uptime"
        assert blip_hour.state == State.HEALTHY, (
            f"single-blip hour should stay HEALTHY, got {blip_hour.state.label} "
            f"(uptime={blip_hour.uptime_fraction})"
        )
        # And the incident detector should also NOT flag this — consistency check.
        flat = [i for day in snap.recent_history for i in day.incidents]
        assert flat == [], f"single blip should not produce incidents, got {len(flat)}"

    async def test_multiple_intermittent_failures_render_degraded(self, mock_prom):
        """Two or more SCATTERED unhealthy samples within the same hour
        (each isolated — no consecutive run >=2) should render DEGRADED,
        not HEALTHY (single-sample blips are tolerated, but two in the
        same hour signal real intermittent issues even if not consecutive).
        """
        from datetime import datetime, timezone
        now = time.time()
        step = 900
        # Hour-align the grid so :00/:30 land in the same hour with a healthy
        # :15 between them — two failures, no consecutive run of 2.
        start = (
            datetime.fromtimestamp(now - (14 * 86400), tz=timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
            .timestamp()
        )
        n = int((now - start) // step)
        idx = (3 * 24) * 4  # an hour ~3 days back, idx % 4 == 0 → :00 of that hour
        bad_indices = {idx, idx + 2}  # :00 and :30, with healthy :15 between
        samples = [
            (start + i * step, 0.0 if i in bad_indices else 1.0)
            for i in range(n)
        ]
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0},
            ).get()
        finally:
            await prom.close()
        ep = snap.models_by_category["Model"][0].endpoints[0]
        degraded = [c for c in ep.hourly_strip if c.state == State.DEGRADED]
        assert degraded, "two scattered failures in one hour should render DEGRADED"

    async def test_majority_failed_hour_renders_down(self, mock_prom):
        """If an hour has a >=2-consecutive run of fully-failed probes, that
        hour bar is DOWN — a real >=30min outage in one window."""
        now = time.time()
        step = 900
        n = (30 * 86400) // step
        # ~2 days back: a 4-sample (1h) consecutive run all at 0.0 — full hour down.
        day_off = 2 * 96
        bad_indices = {n - day_off - 48, n - day_off - 47, n - day_off - 46, n - day_off - 45}
        samples = [
            (now - (30 * 86400) + i * step, 0.0 if i in bad_indices else 1.0)
            for i in range(n)
        ]
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", samples)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0},
            ).get()
        finally:
            await prom.close()
        ep = snap.models_by_category["Model"][0].endpoints[0]
        down = [c for c in ep.hourly_strip if c.state == State.DOWN]
        assert down, "an hour-long full outage should render that hour DOWN"

    async def test_latency_over_slo_renders_degraded_bar(self, mock_prom):
        """An hour that was up (all probes passed) but slow (p95 > SLO) renders
        DEGRADED (yellow) via the latency-aware bar path — never DOWN."""
        now = time.time()
        step = 900
        n = (14 * 86400) // step
        start = now - (14 * 86400)
        # Every probe succeeds → success rule alone makes every hour HEALTHY.
        success = [(start + i * step, 1.0) for i in range(n)]
        # p95 latency under the 5s SLO everywhere except a spike in one recent hour.
        slow_i = n - 10
        latency = [(start + i * step, 12.0 if i == slow_i else 2.0) for i in range(n)]
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", success)],
            latency_range_data=[_matrix("m1", "external", latency)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0}, strip_days=14,
            ).get()
        finally:
            await prom.close()
        ep = snap.models_by_category["Model"][0].endpoints[0]
        degraded = [c for c in ep.hourly_strip if c.state == State.DEGRADED]
        assert degraded, "an up-but-slow hour (p95 > SLO) should render DEGRADED"
        # Up-but-slow is degraded, not an outage — no bar should be DOWN here.
        assert all(c.state != State.DOWN for c in ep.hourly_strip)
        # The bar carries a hover reason that explains the latency cause.
        assert any(c.detail and "latency" in c.detail.lower() for c in degraded), \
            "latency-degraded bar should carry a reason mentioning latency"

    async def test_latency_does_not_mask_outage(self, mock_prom):
        """A genuine outage (>=2 consecutive failures) that is ALSO slow stays
        DOWN — the latency promotion only ever upgrades HEALTHY, never DOWN."""
        from datetime import datetime, timezone
        now = time.time()
        step = 900
        # Hour-align the grid so the two failures land in the same hour bucket
        # (idx % 4 == 0 → :00 and :15) rather than straddling an hour boundary.
        start = (
            datetime.fromtimestamp(now - (14 * 86400), tz=timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
            .timestamp()
        )
        n = int((now - start) // step)
        idx = (2 * 24) * 4
        bad = {idx, idx + 1}  # two consecutive failures within one hour
        success = [(start + i * step, 0.0 if i in bad else 1.0) for i in range(n)]
        latency = [(start + i * step, 30.0) for i in range(n)]  # slow everywhere
        _MockPromHandler.responses = _setup(
            success=[_vector("m1", "external", 1.0)],
            latency=[_vector("m1", "external", 2.0)],
            range_data=[_matrix("m1", "external", success)],
            latency_range_data=[_matrix("m1", "external", latency)],
        )
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(
                prom, {"m1": {"category": "Model"}}, {"m1": 5.0}, strip_days=14,
            ).get()
        finally:
            await prom.close()
        ep = snap.models_by_category["Model"][0].endpoints[0]
        assert any(c.state == State.DOWN for c in ep.hourly_strip), "the outage hour must stay DOWN"

    async def test_internal_failure_surfaces_in_model_state(self, mock_prom):
        """Internal endpoint failure should be reflected at the top-level model row.

        Per user feedback: most consumers are internal, so internal failures
        are user-facing for status purposes.
        """
        _MockPromHandler.responses = {
            "/api/v1/query": [
                ("openserve_probe_success", [
                    _vector("m1", "internal", 0.1),
                    _vector("m1", "external", 1.0),
                ]),
                ("probe_latency_seconds", [
                    _vector("m1", "internal", 2.0),
                    _vector("m1", "external", 2.0),
                ]),
            ],
            "/api/v1/query_range": [(None, [])],
        }
        prom = PrometheusClient(mock_prom)
        try:
            snap = await Snapshotter(prom, {"m1": {"category": "Model"}}, {"m1": 5.0}).get()
        finally:
            await prom.close()
        m = snap.models_by_category["Model"][0]
        assert m.state == State.DOWN  # worst-of-endpoints rule
        # External endpoint sub-state is still healthy.
        ext = next(e for e in m.endpoints if e.endpoint == "external")
        intl = next(e for e in m.endpoints if e.endpoint == "internal")
        assert ext.state == State.HEALTHY
        assert intl.state == State.DOWN
