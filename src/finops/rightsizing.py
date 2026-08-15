"""Turning observed usage into resource-request recommendations.

The arithmetic is simple. What makes this non-trivial is knowing when *not* to
recommend a change:

  * a workload with two days of history has not been observed through a weekly
    peak, so its P95 is not yet the number you want to size against
  * memory is not CPU. An under-provisioned CPU request means throttling; an
    under-provisioned memory limit means OOMKill. So CPU can be trimmed close
    to observed usage while memory keeps real headroom.
  * a recommendation that saves four cents is churn, not savings

A tool that emits a PR for every workload every week gets muted within a
fortnight, and then the genuinely valuable recommendation goes unread with the
rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Below this, the PR costs more attention than the money it saves.
MIN_MONTHLY_SAVING_USD = 5.0

# A week, so the sample spans at least one weekly traffic cycle.
MIN_OBSERVATION_DAYS = 7

# Memory keeps more headroom than CPU on purpose: exceeding a CPU request means
# throttling, exceeding a memory limit means the kernel kills the process.
CPU_HEADROOM = 1.15
MEMORY_HEADROOM = 1.40

# Rough on-demand blended rates. Real numbers come from the CUR pipeline; these
# are the fallback so a recommendation always carries a cost estimate.
CPU_CORE_MONTH_USD = 24.0
MEMORY_GIB_MONTH_USD = 3.2


class Verdict(StrEnum):
    RESIZE = "resize"
    HOLD = "hold"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class Usage:
    """Observed usage for one container over the sampling window."""

    workload: str
    namespace: str
    container: str
    observed_days: float

    cpu_request_cores: float
    cpu_p95_cores: float
    cpu_max_cores: float

    memory_request_gib: float
    memory_p95_gib: float
    memory_max_gib: float

    replicas: int = 1

    def __post_init__(self) -> None:
        if self.replicas < 1:
            raise ValueError(f"{self.workload}: replicas must be at least 1")
        if self.observed_days < 0:
            raise ValueError(f"{self.workload}: observed_days cannot be negative")
        for label, value in (
            ("cpu_request_cores", self.cpu_request_cores),
            ("memory_request_gib", self.memory_request_gib),
        ):
            if value <= 0:
                raise ValueError(f"{self.workload}: {label} must be positive")


@dataclass(frozen=True, slots=True)
class Recommendation:
    usage: Usage
    verdict: Verdict
    cpu_target_cores: float
    memory_target_gib: float
    monthly_saving_usd: float
    reason: str

    @property
    def is_actionable(self) -> bool:
        return self.verdict is Verdict.RESIZE

    @property
    def increases_memory(self) -> bool:
        return self.memory_target_gib > self.usage.memory_request_gib

    def as_dict(self) -> dict[str, object]:
        return {
            "workload": self.usage.workload,
            "namespace": self.usage.namespace,
            "container": self.usage.container,
            "verdict": self.verdict.value,
            "cpu": {
                "current": round(self.usage.cpu_request_cores, 3),
                "target": round(self.cpu_target_cores, 3),
            },
            "memory_gib": {
                "current": round(self.usage.memory_request_gib, 3),
                "target": round(self.memory_target_gib, 3),
            },
            "monthly_saving_usd": round(self.monthly_saving_usd, 2),
            "reason": self.reason,
        }


def monthly_cost(cpu_cores: float, memory_gib: float, replicas: int) -> float:
    return replicas * (cpu_cores * CPU_CORE_MONTH_USD + memory_gib * MEMORY_GIB_MONTH_USD)


def recommend(usage: Usage) -> Recommendation:
    """Produce a recommendation, or explain why there isn't one."""
    if usage.observed_days < MIN_OBSERVATION_DAYS:
        return Recommendation(
            usage=usage,
            verdict=Verdict.INSUFFICIENT_DATA,
            cpu_target_cores=usage.cpu_request_cores,
            memory_target_gib=usage.memory_request_gib,
            monthly_saving_usd=0.0,
            reason=(
                f"only {usage.observed_days:.1f} days of history; "
                f"need {MIN_OBSERVATION_DAYS} to span a weekly peak"
            ),
        )

    # Sized against P95 rather than the max: a single spike should not pin the
    # request forever. But never below the observed max for memory, because
    # that spike is an OOMKill waiting to happen.
    cpu_target = max(usage.cpu_p95_cores * CPU_HEADROOM, 0.01)
    memory_target = max(usage.memory_p95_gib * MEMORY_HEADROOM, usage.memory_max_gib)

    current = monthly_cost(usage.cpu_request_cores, usage.memory_request_gib, usage.replicas)
    proposed = monthly_cost(cpu_target, memory_target, usage.replicas)
    saving = current - proposed

    # An increase is always worth proposing even though it costs money: an
    # under-provisioned memory limit is an incident, not a saving.
    if memory_target > usage.memory_request_gib:
        return Recommendation(
            usage=usage,
            verdict=Verdict.RESIZE,
            cpu_target_cores=cpu_target,
            memory_target_gib=memory_target,
            monthly_saving_usd=saving,
            reason=(
                f"memory request is below observed peak "
                f"({usage.memory_max_gib:.2f} GiB); raising it to avoid OOMKill"
            ),
        )

    if saving < MIN_MONTHLY_SAVING_USD:
        return Recommendation(
            usage=usage,
            verdict=Verdict.HOLD,
            cpu_target_cores=usage.cpu_request_cores,
            memory_target_gib=usage.memory_request_gib,
            monthly_saving_usd=saving,
            reason=(
                f"saving of ${saving:.2f}/month is below the ${MIN_MONTHLY_SAVING_USD:.0f} "
                f"threshold; not worth a pull request"
            ),
        )

    return Recommendation(
        usage=usage,
        verdict=Verdict.RESIZE,
        cpu_target_cores=cpu_target,
        memory_target_gib=memory_target,
        monthly_saving_usd=saving,
        reason=(
            f"P95 usage is {usage.cpu_p95_cores:.2f} cores / {usage.memory_p95_gib:.2f} GiB "
            f"against requests of {usage.cpu_request_cores:.2f} / "
            f"{usage.memory_request_gib:.2f}"
        ),
    )


def summarise(recommendations: list[Recommendation]) -> dict[str, object]:
    actionable = [r for r in recommendations if r.is_actionable]
    return {
        "workloads_examined": len(recommendations),
        "actionable": len(actionable),
        "held": sum(1 for r in recommendations if r.verdict is Verdict.HOLD),
        "insufficient_data": sum(
            1 for r in recommendations if r.verdict is Verdict.INSUFFICIENT_DATA
        ),
        "memory_increases": sum(1 for r in actionable if r.increases_memory),
        # Increases net off against decreases, so this is the real figure
        # rather than the headline one.
        "net_monthly_saving_usd": round(sum(r.monthly_saving_usd for r in actionable), 2),
    }
