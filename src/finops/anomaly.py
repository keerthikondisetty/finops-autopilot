"""Spend anomaly detection over daily cost data.

A rolling median and MAD rather than a mean and standard deviation. Cost series
are full of legitimate spikes (a batch job, a load test, a migration), and a
single one of those inflates the mean and the standard deviation together,
which then hides the *next* anomaly. The median barely moves.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

# 3.5 MADs is roughly a 3-sigma equivalent for normal data but far more
# tolerant of the outliers a cost series actually contains.
DEFAULT_THRESHOLD = 3.5

# Below this, percentage swings are meaningless: a service going from $0.10 to
# $0.60 a day is a 500% increase and nobody cares.
MIN_ABSOLUTE_USD = 20.0

# Scaling factor making MAD comparable to a standard deviation for normal data.
_MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True, slots=True)
class DailyCost:
    day: date
    dimension: str
    amount_usd: float


@dataclass(frozen=True, slots=True)
class Anomaly:
    day: date
    dimension: str
    amount_usd: float
    baseline_usd: float
    deviations: float

    @property
    def excess_usd(self) -> float:
        return self.amount_usd - self.baseline_usd

    @property
    def multiple(self) -> float:
        return self.amount_usd / self.baseline_usd if self.baseline_usd else float("inf")

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "dimension": self.dimension,
            "amount_usd": round(self.amount_usd, 2),
            "baseline_usd": round(self.baseline_usd, 2),
            "excess_usd": round(self.excess_usd, 2),
            "deviations": round(self.deviations, 2),
            "multiple": round(self.multiple, 2),
        }


def median_absolute_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    median = statistics.median(values)
    return statistics.median([abs(v - median) for v in values])


def detect(
    series: list[DailyCost],
    *,
    window: int = 14,
    threshold: float = DEFAULT_THRESHOLD,
    min_absolute_usd: float = MIN_ABSOLUTE_USD,
) -> list[Anomaly]:
    """Find days whose spend departs from the preceding window.

    Only increases are reported. A sudden drop is usually a workload being
    switched off deliberately, and alerting on it trains people to ignore the
    alert that matters.
    """
    if window < 3:
        raise ValueError("window must be at least 3 days to have a baseline")

    by_dimension: dict[str, list[DailyCost]] = {}
    for point in series:
        by_dimension.setdefault(point.dimension, []).append(point)

    anomalies: list[Anomaly] = []
    for dimension, points in by_dimension.items():
        points.sort(key=lambda p: p.day)
        for index in range(window, len(points)):
            history = [p.amount_usd for p in points[index - window : index]]
            current = points[index]

            baseline = statistics.median(history)
            mad = median_absolute_deviation(history)

            if current.amount_usd <= baseline:
                continue
            if current.amount_usd < min_absolute_usd:
                continue

            if mad == 0:
                # A perfectly flat history. Any change is infinitely many MADs,
                # which is useless, so fall back to a relative test.
                if current.amount_usd < baseline * 1.5:
                    continue
                deviations = float("inf")
            else:
                deviations = (current.amount_usd - baseline) / (mad * _MAD_TO_SIGMA)
                if deviations < threshold:
                    continue

            anomalies.append(
                Anomaly(
                    day=current.day,
                    dimension=dimension,
                    amount_usd=current.amount_usd,
                    baseline_usd=baseline,
                    deviations=deviations,
                )
            )

    anomalies.sort(key=lambda a: a.excess_usd, reverse=True)
    return anomalies
