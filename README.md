# finops-autopilot

Spend anomaly detection and rightsizing recommendations, built around a
principle most cost tools ignore: knowing when to stay quiet.

[![verify](https://github.com/keerthikondisetty/finops-autopilot/actions/workflows/verify.yml/badge.svg)](https://github.com/keerthikondisetty/finops-autopilot/actions/workflows/verify.yml)
[![tests](https://img.shields.io/badge/pytest-28%20passed%2C%2098%25-brightgreen)](tests)

## The failure mode this is designed around

A cost tool that opens a pull request for every workload every week gets muted
within a fortnight. After that the one recommendation that actually mattered
goes unread along with everything else.

So the interesting logic here is all in the negative cases:

- **Under a week of history?** No recommendation. A workload observed for three
  days hasn't been through a weekly peak, so its P95 isn't the number you want
  to size against yet.
- **Saving under $5/month?** Held back. That's churn, not savings.
- **Cost dropped sharply?** Not reported. Somebody turned a workload off
  deliberately, and alerting on it teaches people to ignore the alert.
- **Service went from $0.10 to $0.60 a day?** Ignored. A 500% increase nobody
  cares about.

## Memory is not CPU

Both get right-sized, with different headroom on purpose:

```python
CPU_HEADROOM    = 1.15
MEMORY_HEADROOM = 1.40
```

Exceeding a CPU request means throttling. Exceeding a memory limit means the
kernel kills the process. So CPU gets trimmed close to observed usage while
memory keeps real headroom, and the memory target is **never** allowed below
the observed peak regardless of what P95 says.

That also means the tool will sometimes recommend spending *more*: an
under-provisioned memory limit is an incident waiting to happen, and a cost
tool that only ever recommends cuts is a liability.

Which is why the summary reports **net** savings, with increases netted off
against decreases. Reporting gross savings while quietly spending more
elsewhere is the easiest way for a cost tool to lie to you.

## Why median and MAD instead of mean and standard deviation

Cost series are full of legitimate spikes — a batch job, a load test, a
migration. One of those inflates the mean *and* the standard deviation
together, which raises the detection bar and hides the **next** anomaly. The
median barely moves.

There's a test for exactly this: a 9x spike mid-history, then a genuine 4x
anomaly afterwards, which is still caught.

Flat histories get a special case. A perfectly constant series has a MAD of
zero, which would make every subsequent change infinitely many deviations, so
those fall back to a relative test.

Results are ranked by **excess dollars**, not by ratio. A 10x jump on a $2/day
service matters less than 2x on a $500/day one.

## Verification

```bash
make verify
```

```
pytest          28 passed, 98% coverage
mypy --strict   clean
ruff            clean
```

One of those tests caught a real mistake in my own test fixture, which is
worth mentioning because it's the same mistake the tool exists to prevent:
I asserted that recommending more memory would show a net cost *increase*, but
the fixture left CPU heavily over-provisioned, so the CPU saving swamped it and
the total still looked like a saving. The fixture now holds CPU at its
right-sized value. Exactly the accounting error that lets a dashboard claim
savings it isn't making.

> **Not verified here:** nothing runs against real AWS. The CUR pipeline and
> the idle-reaper Lambda are described but the maths is what's tested, because
> the maths is the part that produces wrong numbers quietly.

## Cost as a review-time signal

`.github/workflows/cost-delta.yml` comments the estimated monthly cost change
on every infrastructure PR. A reviewer who can see `+$340/month` argues about
it before it ships rather than at month end.

## Gaps

The CUR 2.0 → Athena pipeline and the tag-driven idle reaper are designed but
not built. The reaper especially needs its dry-run, grace-period and opt-out
tag path implemented carefully before it deletes anything.

Shared-cost allocation isn't handled. Splitting a NAT gateway or a control
plane fairly across tenants is a genuinely hard problem and this doesn't
attempt it.

Rates are hardcoded blended on-demand figures. Real numbers should come from
the pricing API, and Savings Plan and spot coverage should change them.

No unit economics. Cost per 1,000 requests per service is the number that
makes spend arguable; total spend on its own mostly isn't.

---

Reads usage from `otel-observability-platform` and opens rightsizing PRs
against `eks-gitops-platform`. Bills accrue in accounts governed by
`aws-platform-foundation`.

MIT licensed.
