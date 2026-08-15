"""Tests for rightsizing and anomaly detection.

The interesting cases are the ones where the tool should stay quiet. A cost
tool that emits a recommendation for every workload every week gets muted, and
then the one that mattered goes unread with the rest.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from finops.anomaly import DailyCost, detect, median_absolute_deviation
from finops.rightsizing import (
    MIN_OBSERVATION_DAYS,
    Usage,
    Verdict,
    monthly_cost,
    recommend,
    summarise,
)


def usage(**overrides) -> Usage:
    defaults = {
        "workload": "checkout",
        "namespace": "tenant-payments",
        "container": "app",
        "observed_days": 30.0,
        "cpu_request_cores": 2.0,
        "cpu_p95_cores": 0.4,
        "cpu_max_cores": 0.9,
        "memory_request_gib": 4.0,
        "memory_p95_gib": 1.2,
        "memory_max_gib": 1.8,
        "replicas": 6,
    }
    defaults.update(overrides)
    return Usage(**defaults)  # type: ignore[arg-type]


class TestRightsizing:
    def test_over_provisioned_workload_is_resized(self):
        result = recommend(usage())

        assert result.verdict is Verdict.RESIZE
        assert result.cpu_target_cores < 2.0
        assert result.monthly_saving_usd > 0

    def test_short_history_yields_no_recommendation(self):
        """Under a week has not spanned a weekly peak."""
        result = recommend(usage(observed_days=3.0))

        assert result.verdict is Verdict.INSUFFICIENT_DATA
        assert result.cpu_target_cores == 2.0  # unchanged
        assert "weekly peak" in result.reason

    @pytest.mark.parametrize("days", [0.0, 6.9])
    def test_threshold_is_a_full_week(self, days):
        assert recommend(usage(observed_days=days)).verdict is Verdict.INSUFFICIENT_DATA

    def test_exactly_a_week_is_enough(self):
        result = recommend(usage(observed_days=float(MIN_OBSERVATION_DAYS)))

        assert result.verdict is not Verdict.INSUFFICIENT_DATA

    def test_trivial_saving_is_held_back(self):
        """A four-cent saving is churn, not savings."""
        result = recommend(
            usage(
                cpu_request_cores=0.11,
                cpu_p95_cores=0.10,
                memory_request_gib=0.3,
                memory_p95_gib=0.2,
                memory_max_gib=0.21,
                replicas=1,
            )
        )

        assert result.verdict is Verdict.HOLD
        assert "below the $5 threshold" in result.reason

    def test_memory_target_never_drops_below_observed_peak(self):
        """Sizing memory off P95 alone is an OOMKill waiting to happen."""
        result = recommend(usage(memory_p95_gib=1.0, memory_max_gib=3.5))

        assert result.memory_target_gib >= 3.5

    def test_under_provisioned_memory_is_raised_even_though_it_costs_more(self):
        # CPU is held at its right-sized value so the memory increase is the
        # only thing moving the cost. With CPU left over-provisioned the CPU
        # saving swamps the memory increase and the total still looks positive.
        result = recommend(
            usage(
                cpu_request_cores=0.5,
                cpu_p95_cores=0.43,
                memory_request_gib=1.0,
                memory_p95_gib=1.4,
                memory_max_gib=2.6,
            )
        )

        assert result.verdict is Verdict.RESIZE
        assert result.increases_memory
        assert result.monthly_saving_usd < 0  # deliberately spending more
        assert "OOMKill" in result.reason

    def test_memory_keeps_more_headroom_than_cpu(self):
        """Exceeding a CPU request throttles; exceeding a memory limit kills."""
        result = recommend(usage(cpu_p95_cores=1.0, memory_p95_gib=1.0, memory_max_gib=1.0))

        cpu_ratio = result.cpu_target_cores / 1.0
        memory_ratio = result.memory_target_gib / 1.0
        assert memory_ratio > cpu_ratio

    def test_saving_scales_with_replicas(self):
        one = recommend(usage(replicas=1)).monthly_saving_usd
        ten = recommend(usage(replicas=10)).monthly_saving_usd

        assert ten == pytest.approx(one * 10)

    @pytest.mark.parametrize(
        "field,value",
        [("replicas", 0), ("cpu_request_cores", 0), ("memory_request_gib", -1)],
    )
    def test_rejects_impossible_inputs(self, field, value):
        with pytest.raises(ValueError):
            usage(**{field: value})

    def test_monthly_cost_is_linear(self):
        assert monthly_cost(1.0, 1.0, 2) == pytest.approx(monthly_cost(1.0, 1.0, 1) * 2)


class TestSummary:
    def test_counts_each_verdict(self):
        results = [
            recommend(usage()),
            recommend(usage(observed_days=2.0)),
            recommend(
                usage(
                    cpu_request_cores=0.11,
                    cpu_p95_cores=0.10,
                    memory_request_gib=0.3,
                    memory_p95_gib=0.2,
                    memory_max_gib=0.21,
                    replicas=1,
                )
            ),
        ]
        summary = summarise(results)

        assert summary["workloads_examined"] == 3
        assert summary["actionable"] == 1
        assert summary["insufficient_data"] == 1
        assert summary["held"] == 1

    def test_increases_net_off_against_decreases(self):
        """Reporting gross savings while quietly spending more elsewhere is
        the easiest way for a cost tool to lie."""
        results = [
            recommend(usage()),
            recommend(
                usage(
                    workload="reporting",
                    cpu_request_cores=0.5,
                    cpu_p95_cores=0.43,
                    memory_request_gib=1.0,
                    memory_p95_gib=1.4,
                    memory_max_gib=2.6,
                )
            ),
        ]
        summary = summarise(results)

        gross = sum(r.monthly_saving_usd for r in results if r.monthly_saving_usd > 0)
        assert summary["net_monthly_saving_usd"] < gross
        assert summary["memory_increases"] == 1


def series(values: list[float], dimension: str = "checkout") -> list[DailyCost]:
    start = date(2026, 7, 1)
    return [
        DailyCost(day=start + timedelta(days=i), dimension=dimension, amount_usd=v)
        for i, v in enumerate(values)
    ]


class TestAnomalyDetection:
    def test_flags_a_genuine_spike(self):
        anomalies = detect(series([100.0] * 14 + [400.0]))

        assert len(anomalies) == 1
        assert anomalies[0].amount_usd == 400.0
        assert anomalies[0].excess_usd == pytest.approx(300.0)

    def test_ignores_normal_variation(self):
        noisy = [100.0, 105.0, 95.0, 110.0, 90.0] * 3
        anomalies = detect(noisy_series := series(noisy + [108.0]))

        assert anomalies == []
        assert len(noisy_series) == 16

    def test_a_prior_spike_does_not_mask_the_next_one(self):
        """The reason for median and MAD rather than mean and standard
        deviation: one legitimate spike must not raise the bar so far that the
        next anomaly slips under it."""
        history = [100.0] * 7 + [900.0] + [100.0] * 6
        anomalies = detect(series(history + [420.0]))

        assert len(anomalies) == 1
        assert anomalies[0].amount_usd == 420.0

    def test_drops_are_not_reported(self):
        """Turning a workload off is usually deliberate."""
        assert detect(series([100.0] * 14 + [5.0])) == []

    def test_small_absolute_amounts_are_ignored(self):
        """$0.10 to $0.60 is a 500% increase nobody cares about."""
        assert detect(series([0.10] * 14 + [0.60])) == []

    def test_flat_history_uses_a_relative_test(self):
        """A perfectly flat series has MAD zero; every change would otherwise
        be infinitely many deviations."""
        assert detect(series([100.0] * 14 + [110.0])) == []
        assert len(detect(series([100.0] * 14 + [200.0]))) == 1

    def test_dimensions_are_scored_independently(self):
        combined = series([100.0] * 14 + [400.0], "checkout") + series(
            [50.0] * 15, "catalog"
        )
        anomalies = detect(combined)

        assert [a.dimension for a in anomalies] == ["checkout"]

    def test_results_are_ranked_by_money_not_by_ratio(self):
        """A 10x jump on a $2/day service matters less than 2x on a $500 one."""
        combined = series([10.0] * 14 + [120.0], "small") + series(
            [500.0] * 14 + [1400.0], "large"
        )
        anomalies = detect(combined)

        assert [a.dimension for a in anomalies] == ["large", "small"]

    def test_short_series_yields_nothing(self):
        assert detect(series([100.0] * 5)) == []

    def test_rejects_a_useless_window(self):
        with pytest.raises(ValueError, match="at least 3 days"):
            detect(series([100.0] * 20), window=2)

    def test_mad_of_empty_is_zero(self):
        assert median_absolute_deviation([]) == 0.0

    def test_anomaly_serialises(self):
        import json

        anomaly = detect(series([100.0] * 14 + [400.0]))[0]
        payload = json.loads(json.dumps(anomaly.as_dict()))

        assert payload["multiple"] == 4.0
        assert payload["dimension"] == "checkout"
