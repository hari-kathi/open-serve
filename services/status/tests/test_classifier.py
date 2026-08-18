"""Layer 1 — pure unit tests for the state classifier and banner derivation.

These verify the three-tier (green/orange/red) thresholds are honored
without needing Prometheus or any I/O.
"""

import itertools

import pytest
from main import State, banner_from, classify


class TestClassify:
    @pytest.mark.parametrize(
        "success,p95,slo,expected",
        [
            # Healthy: full success, latency under SLO
            (1.0, 2.0, 5.0, State.HEALTHY),
            # Healthy: latency exactly at SLO
            (1.0, 5.0, 5.0, State.HEALTHY),
            # Healthy: no latency data
            (1.0, None, 5.0, State.HEALTHY),

            # Degraded: latency just over SLO
            (1.0, 5.1, 5.0, State.DEGRADED),
            # Degraded: 90% success
            (0.9, 2.0, 5.0, State.DEGRADED),
            # Degraded: 50% boundary
            (0.5, 2.0, 5.0, State.DEGRADED),

            # Down: <50% success
            (0.4, 2.0, 5.0, State.DOWN),
            (0.0, 2.0, 5.0, State.DOWN),

            # Unknown: no success-rate data
            (None, None, 5.0, State.UNKNOWN),
        ],
    )
    def test_thresholds(self, success, p95, slo, expected):
        assert classify(success, p95, slo) == expected


class TestBannerFrom:
    def test_empty_models_renders_healthy_not_unknown(self):
        assert banner_from([]) == State.HEALTHY

    def test_all_healthy(self):
        assert banner_from([State.HEALTHY, State.HEALTHY, State.HEALTHY]) == State.HEALTHY

    def test_one_degraded(self):
        assert banner_from([State.HEALTHY, State.DEGRADED, State.HEALTHY]) == State.DEGRADED

    def test_one_down_outranks_degraded(self):
        assert banner_from([State.HEALTHY, State.DEGRADED, State.DOWN]) == State.DOWN

    @pytest.mark.parametrize(
        "states",
        list(itertools.product([State.HEALTHY, State.DEGRADED, State.DOWN], repeat=3)),
    )
    def test_max_combinations(self, states):
        assert banner_from(list(states)) == max(states)
