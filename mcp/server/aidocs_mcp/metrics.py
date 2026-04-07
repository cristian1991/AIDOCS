"""Prometheus-compatible metrics collector for AIDOCS MCP server.

Collects counters and gauges for:
- Token usage (input/output, by model, by tool)
- Tool call counts (by tool name, by status)
- Output guard findings (by category, by severity)
- Session activity

Exposes metrics in Prometheus text exposition format via:
- `metrics_snapshot()` — returns dict for MCP tool / dashboard API
- `render_prometheus()` — returns text/plain in Prometheus format
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class _CounterValue:
    value: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, amount: float = 1.0) -> None:
        with self.lock:
            self.value += amount

    def get(self) -> float:
        return self.value


class _LabeledCounter:
    """Counter with label dimensions — e.g. tool_calls_total{tool="code_find", status="completed"}."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...]) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], _CounterValue] = {}
        self._lock = threading.Lock()

    def labels(self, *label_values: str) -> _CounterValue:
        key = label_values
        if key not in self._values:
            with self._lock:
                if key not in self._values:
                    self._values[key] = _CounterValue()
        return self._values[key]

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        self.labels(*label_values).inc(amount)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        for label_values, counter in sorted(self._values.items()):
            label_pairs = ",".join(
                f'{name}="{val}"' for name, val in zip(self.label_names, label_values)
            )
            lines.append(f"{self.name}{{{label_pairs}}} {counter.get()}")
        return "\n".join(lines)

    def snapshot(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for label_values, counter in sorted(self._values.items()):
            key = "|".join(f"{n}={v}" for n, v in zip(self.label_names, label_values))
            result[key] = counter.get()
        return result


class _Gauge:
    """Simple gauge (can go up and down)."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._value: float = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    def get(self) -> float:
        return self._value

    def render(self) -> str:
        return (
            f"# HELP {self.name} {self.help_text}\n"
            f"# TYPE {self.name} gauge\n"
            f"{self.name} {self._value}"
        )


# ── Global metrics registry ──

class MetricsCollector:
    """Thread-safe metrics collector with Prometheus exposition format."""

    def __init__(self) -> None:
        self._start_time = time.monotonic()

        # Token counters
        self.tokens_in = _LabeledCounter(
            "aidocs_tokens_input_total",
            "Total estimated input tokens consumed by tool results.",
            ("tool", "session"),
        )
        self.tokens_out = _LabeledCounter(
            "aidocs_tokens_output_total",
            "Total estimated output tokens from tool arguments.",
            ("tool", "session"),
        )

        # Tool call counters
        self.tool_calls = _LabeledCounter(
            "aidocs_tool_calls_total",
            "Total MCP tool calls by tool name and status.",
            ("tool", "status"),
        )

        # Output guard counters
        self.guard_scans = _LabeledCounter(
            "aidocs_output_guard_scans_total",
            "Total output guard scans by result.",
            ("result",),  # "clean", "findings", "redacted"
        )
        self.guard_findings = _LabeledCounter(
            "aidocs_output_guard_findings_total",
            "Total output guard findings by category and severity.",
            ("category", "severity"),
        )

        # Session gauges
        self.active_sessions = _Gauge(
            "aidocs_active_sessions",
            "Number of currently active managed sessions.",
        )

        # All metrics for iteration
        self._counters: list[_LabeledCounter] = [
            self.tokens_in,
            self.tokens_out,
            self.tool_calls,
            self.guard_scans,
            self.guard_findings,
        ]
        self._gauges: list[_Gauge] = [self.active_sessions]

    def record_tool_call(
        self,
        tool_name: str,
        status: str,
        session_id: str = "",
        tokens_in_estimate: int = 0,
        tokens_out_estimate: int = 0,
    ) -> None:
        self.tool_calls.inc(tool_name, status)
        if tokens_in_estimate > 0:
            self.tokens_in.inc(tool_name, session_id or "_none", amount=tokens_in_estimate)
        if tokens_out_estimate > 0:
            self.tokens_out.inc(tool_name, session_id or "_none", amount=tokens_out_estimate)

    def record_guard_scan(
        self,
        clean: bool,
        redaction_count: int = 0,
        findings: list[dict[str, str]] | None = None,
    ) -> None:
        if clean:
            self.guard_scans.inc("clean")
        elif redaction_count > 0:
            self.guard_scans.inc("redacted")
        else:
            self.guard_scans.inc("findings")
        for f in findings or []:
            cat = f.get("category", "unknown")
            sev = f.get("severity", "unknown")
            self.guard_findings.inc(cat, sev)

    def render_prometheus(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        uptime = time.monotonic() - self._start_time
        sections = [
            "# HELP aidocs_uptime_seconds Time since MCP server started.",
            "# TYPE aidocs_uptime_seconds gauge",
            f"aidocs_uptime_seconds {uptime:.1f}",
        ]
        for counter in self._counters:
            rendered = counter.render()
            if rendered.count("\n") >= 2:  # has at least one data line beyond HELP/TYPE
                sections.append(rendered)
        for gauge in self._gauges:
            sections.append(gauge.render())
        return "\n\n".join(sections) + "\n"

    def snapshot(self) -> dict[str, object]:
        """Return a structured dict for MCP tool / dashboard consumption."""
        uptime = time.monotonic() - self._start_time
        return {
            "uptime_seconds": round(uptime, 1),
            "tokens_in": self.tokens_in.snapshot(),
            "tokens_out": self.tokens_out.snapshot(),
            "tool_calls": self.tool_calls.snapshot(),
            "guard_scans": self.guard_scans.snapshot(),
            "guard_findings": self.guard_findings.snapshot(),
            "active_sessions": self.active_sessions.get(),
        }


# Singleton — shared across the MCP server process
_COLLECTOR: MetricsCollector | None = None
_COLLECTOR_LOCK = threading.Lock()


def get_collector() -> MetricsCollector:
    global _COLLECTOR
    if _COLLECTOR is None:
        with _COLLECTOR_LOCK:
            if _COLLECTOR is None:
                _COLLECTOR = MetricsCollector()
    return _COLLECTOR
