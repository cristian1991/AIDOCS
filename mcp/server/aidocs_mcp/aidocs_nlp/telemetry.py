"""NLP telemetry — latency budgets, cache hit rates, consumer fire rates.

Recorded in-memory per-process; dashboard reads via NLPService.snapshot().
For cross-process aggregation (metrics_prometheus endpoint), serialize
the snapshot into the existing metrics surface.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class _LatencyHistogram:
    """Fixed-size ring of recent durations in ms. Computes p50/p95/p99
    over the window. Default window 1024 samples — short enough to be
    responsive, long enough to smooth single outliers.
    """

    window: int = 1024
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=1024))

    def record(self, ms: float) -> None:
        self.samples.append(ms)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        s = sorted(self.samples)
        idx = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
        return s[idx]

    def snapshot(self) -> dict[str, float]:
        if not self.samples:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
        return {
            "count": len(self.samples),
            "p50": round(self.percentile(0.50), 2),
            "p95": round(self.percentile(0.95), 2),
            "p99": round(self.percentile(0.99), 2),
        }


@dataclass
class _ConsumerStats:
    calls: int = 0
    fires: int = 0  # how many calls produced a positive signal

    def snapshot(self) -> dict[str, int]:
        return {"calls": self.calls, "fires": self.fires}


class Telemetry:
    """Per-process NLP telemetry."""

    def __init__(self, analyze_budget_ms: float = 50.0):
        self._analyze_budget_ms = analyze_budget_ms
        self._analyze_lat: dict[str, _LatencyHistogram] = defaultdict(_LatencyHistogram)
        self._consumer_stats: dict[str, _ConsumerStats] = defaultdict(_ConsumerStats)
        self._budget_breaches: int = 0
        self._budget_breach_window: deque[float] = deque(maxlen=200)

    def record_analyze(self, source: str, language: str, ms: float) -> None:
        key = f"{source}|{language}"
        self._analyze_lat[key].record(ms)
        if ms > self._analyze_budget_ms:
            self._budget_breaches += 1
            self._budget_breach_window.append(time.time())

    def record_consumer(self, name: str, fired: bool) -> None:
        s = self._consumer_stats[name]
        s.calls += 1
        if fired:
            s.fires += 1

    def budget_breach_rate(self, window_s: float = 600.0) -> float:
        """Fraction of analyze() calls in the last `window_s` seconds
        that exceeded the budget. Dashboard surfaces a warning when
        this exceeds 0.05.
        """
        now = time.time()
        recent = sum(1 for t in self._budget_breach_window if (now - t) <= window_s)
        total = sum(h.snapshot()["count"] for h in self._analyze_lat.values())
        return (recent / total) if total > 0 else 0.0

    def snapshot(self) -> dict:
        return {
            "analyze_budget_ms": self._analyze_budget_ms,
            "analyze_latency": {key: hist.snapshot() for key, hist in self._analyze_lat.items()},
            "consumers": {name: s.snapshot() for name, s in self._consumer_stats.items()},
            "budget_breaches_total": self._budget_breaches,
            "budget_breach_rate_10m": round(self.budget_breach_rate(600.0), 4),
        }
