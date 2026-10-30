"""Speed and errors suite: client-side latency, budget share, timeouts and error rate.

Reads ``ctx.latencies_ms`` and ``ctx.errors`` (aligned with the samples). When the runner did
not record them the metrics are ``None`` (not measured), never 0. Warm-up is the runner's
concern, not this suite's.
"""

from __future__ import annotations

from typing import Any, Sequence

from .base import HIGHER, LOWER, Metric, SuiteContext, SuiteScore, mean

DEFAULT_BUDGET_MS = 1000.0
_TIMEOUT_WORDS = ("timeout", "timed out")


def is_timeout(error: str | None) -> bool:
    """An error string that contains "timeout" or "timed out" (any case) is a timeout."""
    return bool(error) and any(w in error.lower() for w in _TIMEOUT_WORDS)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100); ``None`` for no values."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


class SpeedSuite:
    name = "speed"
    version = "1"
    needs_judge = False

    def __init__(self, budget_ms: float = DEFAULT_BUDGET_MS) -> None:
        if budget_ms <= 0:
            raise ValueError("budget_ms must be positive")
        self.budget_ms = float(budget_ms)
        b = f"{self.budget_ms:g}"
        ok = "over successful retrievals only (a failed retrieval has no usable latency)"
        self.metrics = (
            Metric(
                "latency_p50_ms",
                f"Median client-side latency of the retrieve call in milliseconds, {ok}. "
                "Client latency is noisy: the first query is cold and the gateway is shared.",
                (0.0, None),
                LOWER,
            ),
            Metric(
                "latency_p95_ms",
                f"95th-percentile (linear interpolation) client-side latency in milliseconds, "
                f"{ok}. Noisy for the same reasons as the median.",
                (0.0, None),
                LOWER,
            ),
            Metric(
                "within_budget",
                f"Share of successful retrievals that finished within the latency budget of "
                f"{b} ms (default {DEFAULT_BUDGET_MS:g} ms, configurable).",
                (0.0, 1.0),
                HIGHER,
            ),
            Metric(
                "timeout_rate",
                "Share of questions whose retrieval failed with an error containing "
                '"timeout" or "timed out" (any case), out of all questions.',
                (0.0, 1.0),
                LOWER,
            ),
            Metric(
                "error_rate",
                "Share of questions whose retrieval failed with any error (timeouts "
                "included), out of all questions.",
                (0.0, 1.0),
                LOWER,
            ),
        )

    def _row(self, lat: float | None, err: Any, have_errors: bool) -> dict[str, float | None]:
        """One question's row; ``lat`` is None unless the retrieval was timed and succeeded."""
        within: float | None = None
        if lat is not None:
            within = 1.0 if lat <= self.budget_ms else 0.0
        return {
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "within_budget": within,
            "timeout_rate": float(is_timeout(err)) if have_errors else None,
            "error_rate": float(bool(err)) if have_errors else None,
        }

    def score(self, ctx: SuiteContext) -> SuiteScore:
        n = len(ctx.samples)
        lats = list(ctx.latencies_ms) if len(ctx.latencies_ms) == n else [None] * n
        errs = list(ctx.errors) if len(ctx.errors) == n else None
        per: list[dict[str, float | None]] = []
        good: list[float] = []
        for i in range(n):
            err = errs[i] if errs is not None else None
            lat = lats[i]
            usable = lat is not None and not err
            if usable:
                good.append(lat)
            per.append(self._row(lat if usable else None, err, errs is not None))
        agg = {
            "latency_p50_ms": percentile(good, 50),
            "latency_p95_ms": percentile(good, 95),
            "within_budget": mean([row["within_budget"] for row in per]),
            "timeout_rate": mean([row["timeout_rate"] for row in per]),
            "error_rate": mean([row["error_rate"] for row in per]),
        }
        return SuiteScore(per, agg)


SUITE = SpeedSuite()
