"""Layer 1 — incident detection from collapsed probe-success runs."""

from main import State, _collapse_runs


def _samples(values: list[tuple[float, float]]) -> list[list[float]]:
    return [[ts, str(v)] for ts, v in values]


class TestCollapseRuns:
    def test_no_runs_when_all_healthy(self):
        samples = _samples([(100.0, 1.0), (1000.0, 1.0), (1900.0, 1.0)])
        assert _collapse_runs(samples) == []

    def test_single_down_run(self):
        samples = _samples([(100.0, 1.0), (1000.0, 0.0), (1900.0, 0.0), (2800.0, 1.0)])
        runs = _collapse_runs(samples)
        assert len(runs) == 1
        start, end, peak, count = runs[0]
        assert start == 1000.0
        assert end == 1900.0
        assert peak == State.DOWN
        assert count == 2

    def test_sample_count_used_to_filter_single_blip(self):
        # A SINGLE failed sample → run with sample_count=1. Caller can drop these.
        samples = _samples([(100.0, 1.0), (1000.0, 0.0), (1900.0, 1.0)])
        runs = _collapse_runs(samples)
        assert len(runs) == 1
        _, _, peak, count = runs[0]
        assert peak == State.DOWN
        assert count == 1

    def test_ongoing_run_no_recovery(self):
        samples = _samples([(100.0, 1.0), (1000.0, 0.4), (1900.0, 0.3)])
        runs = _collapse_runs(samples)
        assert len(runs) == 1
        start, end, peak, count = runs[0]
        assert start == 1000.0
        assert end == 1900.0
        assert peak == State.DOWN
        assert count == 2

    def test_peak_state_promotes_to_worst_observed(self):
        samples = _samples([(100.0, 0.8), (1000.0, 0.2), (1900.0, 1.0)])
        runs = _collapse_runs(samples)
        _, _, peak, _ = runs[0]
        assert peak == State.DOWN

    def test_degraded_only_run(self):
        samples = _samples([(100.0, 1.0), (1000.0, 0.7), (1900.0, 0.6), (2800.0, 1.0)])
        runs = _collapse_runs(samples)
        assert len(runs) == 1
        assert runs[0][2] == State.DEGRADED
        assert runs[0][3] == 2

    def test_multiple_distinct_runs(self):
        samples = _samples([
            (100.0, 1.0),
            (1000.0, 0.0),
            (1900.0, 1.0),
            (2800.0, 0.7),
            (3700.0, 1.0),
        ])
        runs = _collapse_runs(samples)
        assert len(runs) == 2
        assert runs[0][2] == State.DOWN
        assert runs[1][2] == State.DEGRADED

    def test_malformed_samples_skipped(self):
        samples = [[100.0, "1.0"], [200.0, "not-a-number"], [300.0]]
        assert _collapse_runs(samples) == []
