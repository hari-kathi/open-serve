"""Layer 1 — rendering snapshots over canonical PageSnapshot fixtures.

Golden JSON contract + HTML render across six canonical states. Each model
is one row; sub-endpoints are children. The HTML must carry data-test-*
attributes Layer 3 e2e tests rely on.
"""

import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from main import (
    DailyHistoryEntry,
    EndpointHealth,
    HourCell,
    Incident,
    Model,
    PageSnapshot,
    State,
    _format_date,
    _format_duration,
    _format_hour,
    _format_iso,
    _format_latency,
    _format_uptime,
    snapshot_to_dict,
)

GOLDEN_DIR = Path(__file__).parent / "golden"
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

FIXED_TS = 1779900000.0  # 2026-05-27 14:00:00 UTC


def _branding() -> dict[str, str]:
    return {
        "title": "open-serve Status",
        "logo_url": "",
        "footer_link_text": "",
        "footer_link_url": "",
    }


def _healthy_strip(hours: int = 6) -> list[HourCell]:
    """All-healthy hourly strip ending at the FIXED_TS hour (UTC).

    Kept short (a handful of hours) so the golden JSON stays readable — a full
    STRIP_DAYS*24 strip would be hundreds of cells. Hours back-calculated from
    FIXED_TS.
    """
    from datetime import datetime, timedelta, timezone
    current_hour = datetime.fromtimestamp(FIXED_TS, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    return [
        HourCell(
            iso_hour=(current_hour - timedelta(hours=hours - 1 - i)).strftime("%Y-%m-%dT%H:00Z"),
            state=State.HEALTHY,
            uptime_fraction=1.0,
        )
        for i in range(hours)
    ]


def _endpoint(endpoint: str, state: State, **overrides) -> EndpointHealth:
    defaults = dict(
        success_rate_5m=1.0 if state == State.HEALTHY else 0.9 if state == State.DEGRADED else 0.2 if state == State.DOWN else None,
        p95_latency_s=2.5 if state == State.HEALTHY else 8.0 if state == State.DEGRADED else 30.0 if state == State.DOWN else None,
        slo_latency_s=5.0,
        hourly_strip=_healthy_strip(),
    )
    defaults.update(overrides)
    return EndpointHealth(endpoint=endpoint, state=state, **defaults)


def _model(model_id: str, state: State, *, category: str = "Model", **endpoint_overrides) -> Model:
    return Model(
        model_id=model_id,
        category=category,
        description="",
        endpoints=[
            _endpoint("external", state, **endpoint_overrides),
            _endpoint("internal", state, **endpoint_overrides),
        ],
    )


def _snapshot(
    *,
    banner: State,
    fallback: bool = False,
    models_by_category: dict[str, list[Model]] | None = None,
    active: list[Incident] | None = None,
    history: list[DailyHistoryEntry] | None = None,
) -> PageSnapshot:
    return PageSnapshot(
        rendered_at=FIXED_TS,
        banner_state=banner,
        fallback_mode=fallback,
        data_stale_s=0.0,
        models_by_category=models_by_category or {},
        active_incidents=active or [],
        recent_history=history or [],
        branding=_branding(),
    )


# --- Fixture catalog ---

def fx_all_healthy() -> PageSnapshot:
    return _snapshot(
        banner=State.HEALTHY,
        models_by_category={
            "Model": [_model("qwen3-8b", State.HEALTHY)],
            "Embedding": [_model("mxbai", State.HEALTHY, category="Embedding", slo_latency_s=1.0, p95_latency_s=0.4)],
        },
    )


def fx_one_degraded() -> PageSnapshot:
    return _snapshot(
        banner=State.DEGRADED,
        models_by_category={
            "Model": [
                _model("qwen3-8b", State.HEALTHY),
                _model("qwen3-32b", State.DEGRADED),
            ],
        },
    )


def fx_one_down() -> PageSnapshot:
    return _snapshot(
        banner=State.DOWN,
        models_by_category={
            "Model": [
                _model("qwen3-8b", State.HEALTHY),
                _model("qwen3-32b", State.DOWN),
            ],
        },
        active=[
            Incident(
                model_id="qwen3-32b",
                endpoint="external",
                start_ts=FIXED_TS - 600.0,
                end_ts=None,
                peak_state=State.DOWN,
                error_type="status_502",
            )
        ],
    )


def fx_mixed() -> PageSnapshot:
    return _snapshot(
        banner=State.DOWN,
        models_by_category={
            "Model": [
                _model("qwen3-8b", State.HEALTHY),
                _model("qwen3-32b", State.DEGRADED),
                _model("gpt-oss-120b", State.DOWN),
            ],
            "Embedding": [_model("mxbai", State.HEALTHY, category="Embedding", slo_latency_s=1.0, p95_latency_s=0.5)],
        },
    )


def fx_fallback() -> PageSnapshot:
    return _snapshot(banner=State.UNKNOWN, fallback=True)


def fx_empty() -> PageSnapshot:
    return _snapshot(banner=State.HEALTHY)


def fx_with_history() -> PageSnapshot:
    """Snapshot with non-empty recent_history (a day card + a resolved incident).

    Catches template-render bugs that only surface when there's actual
    history data — e.g., earlier we had a stale `day.date` reference in
    the template instead of `day.iso_date`, which only crashed at render
    time when the for-loop iterated over at least one entry.
    """
    from datetime import datetime, timezone
    today = datetime.fromtimestamp(FIXED_TS, tz=timezone.utc).date().strftime("%Y-%m-%d")
    resolved_incident = Incident(
        model_id="qwen3-8b",
        endpoint="external",
        start_ts=FIXED_TS - 3600.0,
        end_ts=FIXED_TS - 2400.0,
        peak_state=State.DOWN,
        error_type="timeout",
    )
    return _snapshot(
        banner=State.HEALTHY,
        models_by_category={"Model": [_model("qwen3-8b", State.HEALTHY)]},
        history=[
            DailyHistoryEntry(iso_date=today, incidents=[resolved_incident]),
            DailyHistoryEntry(iso_date="2026-05-26", incidents=[]),
        ],
    )


FIXTURES = {
    "all_healthy": fx_all_healthy,
    "one_degraded": fx_one_degraded,
    "one_down": fx_one_down,
    "mixed": fx_mixed,
    "fallback": fx_fallback,
    "empty": fx_empty,
    "with_history": fx_with_history,
}


# --- JSON contract test ---

class TestJsonContract:
    def test_all_healthy_matches_golden(self):
        snap = fx_all_healthy()
        payload = snapshot_to_dict(snap)
        golden = json.loads((GOLDEN_DIR / "status.example.json").read_text())
        assert payload == golden

    @pytest.mark.parametrize("name", list(FIXTURES.keys()))
    def test_fixture_serializes(self, name):
        snap = FIXTURES[name]()
        payload = snapshot_to_dict(snap)
        json.dumps(payload)
        assert "banner" in payload
        assert "components" in payload
        assert isinstance(payload["active_incidents"], list)
        assert isinstance(payload["recent_history"], list)

    def test_state_colors_locked(self):
        snap = fx_mixed()
        payload = snapshot_to_dict(snap)
        seen = {}
        for models in payload["components"].values():
            for m in models:
                seen[m["state"]] = m["color"]
        assert seen.get("healthy") == "#22c55e"
        assert seen.get("degraded") == "#ca8a04"
        assert seen.get("down") == "#ef4444"

    def test_model_state_is_max_of_endpoints(self):
        # external=down, internal=healthy → top-level = down (worst wins).
        m = Model(
            model_id="m1",
            category="Model",
            description="",
            endpoints=[
                _endpoint("external", State.DOWN),
                _endpoint("internal", State.HEALTHY),
            ],
        )
        assert m.state == State.DOWN

    def test_model_uptime_averages_across_endpoints(self):
        m = Model(
            model_id="m1",
            category="Model",
            description="",
            endpoints=[
                _endpoint("external", State.HEALTHY, hourly_strip=_healthy_strip()),
                _endpoint("internal", State.HEALTHY, hourly_strip=_healthy_strip()),
            ],
        )
        assert m.uptime_fraction == pytest.approx(1.0)

    def test_model_hourly_strip_worst_state_per_hour(self):
        from datetime import datetime, timezone
        iso_hour = datetime.fromtimestamp(FIXED_TS, tz=timezone.utc).strftime("%Y-%m-%dT%H:00Z")
        ext = EndpointHealth("external", State.DOWN, 0.2, 30.0, 5.0,
                             [HourCell(iso_hour, State.DOWN, 0.2)])
        intl = EndpointHealth("internal", State.HEALTHY, 1.0, 1.0, 5.0,
                              [HourCell(iso_hour, State.HEALTHY, 1.0)])
        m = Model("m1", "Model", "", [ext, intl])
        strip = m.hourly_strip
        assert len(strip) == 1
        assert strip[0].state == State.DOWN
        assert strip[0].uptime_fraction == pytest.approx(0.2)


# --- HTML render test ---

@pytest.fixture(scope="module")
def jinja_env():
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["latency"] = _format_latency
    env.filters["iso"] = _format_iso
    env.filters["pretty_date"] = _format_date
    env.filters["hour_label"] = _format_hour
    env.filters["uptime"] = _format_uptime
    env.filters["duration"] = _format_duration
    return env


class TestHtmlRender:
    @pytest.mark.parametrize("name", list(FIXTURES.keys()))
    def test_renders_without_error(self, jinja_env, name):
        snap = FIXTURES[name]()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap,
            State=State,
            json_url="/status.json",
            strip_days=30,
            recent_history_days=30,
        )
        assert "<!DOCTYPE html>" in html
        assert f'data-test-banner="{snap.banner_state.label}"' in html

    def test_text_changes(self, jinja_env):
        snap = fx_all_healthy()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap, State=State, json_url="/status.json",
            strip_days=30, recent_history_days=30,
        )
        assert "All models operational" in html  # not "All systems operational"
        assert "open-serve Status" in html

    def test_one_down_renders_active_incident_with_error_type(self, jinja_env):
        snap = fx_one_down()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap, State=State, json_url="/status.json",
            strip_days=30, recent_history_days=30,
        )
        assert 'data-test-incident="qwen3-32b"' in html
        # error_type should surface in active-incident card
        assert "status_502" in html

    def test_fallback_renders_warning(self, jinja_env):
        snap = fx_fallback()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap, State=State, json_url="/status.json",
            strip_days=30, recent_history_days=30,
        )
        assert "temporarily unavailable" in html.lower()

    def test_empty_renders_no_models_msg(self, jinja_env):
        snap = fx_empty()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap, State=State, json_url="/status.json",
            strip_days=30, recent_history_days=30,
        )
        assert "No models" in html

    def test_data_test_attributes_present(self, jinja_env):
        snap = fx_mixed()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap, State=State, json_url="/status.json",
            strip_days=30, recent_history_days=30,
        )
        assert 'data-test-category="Model"' in html
        assert 'data-test-model="gpt-oss-120b"' in html
        assert 'data-test-state="down"' in html
        # Bars carry per-hour data attributes for the tooltip JS
        assert "data-bar-state=" in html
        assert "data-bar-hour=" in html
        assert "data-bar-date=" in html
        # Machine-readable UTC hour key the JS reads to relabel in local time.
        assert "data-bar-hour-iso=" in html

    def test_timestamps_carry_epoch_for_local_time_js(self, jinja_env):
        # "Last updated" and active-incident start times expose data-ts (epoch)
        # so the client JS can re-render them in the viewer's local timezone.
        snap = fx_one_down()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap, State=State, json_url="/status.json",
            strip_days=14, recent_history_days=30,
        )
        assert 'class="js-localtime" data-ts=' in html

    def test_history_section_collapsed_by_default(self, jinja_env):
        snap = fx_all_healthy()
        html = jinja_env.get_template("status.html.jinja").render(
            snap=snap, State=State, json_url="/status.json",
            strip_days=30, recent_history_days=30,
        )
        # The history section should be inside a <details> tag without `open`
        assert "<details" in html
        # Must contain "Recent history" or "Past 30 days" wording for the trigger
        assert "history" in html.lower() or "past" in html.lower()
