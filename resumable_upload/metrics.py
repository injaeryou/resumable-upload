"""Zero-dependency Prometheus text-format metrics registry.

The registry supports only counters — histograms and gauges are deliberately
out of scope. Enough to let operators scrape request/error/byte volumes into
Grafana or Datadog without pulling ``prometheus_client`` into the core.
"""

from __future__ import annotations

import threading


class MetricsRegistry:
    """Thread-safe counter-only Prometheus registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], int]] = {}
        self._help: dict[str, str] = {}

    def register_counter(self, name: str, help_text: str) -> None:
        """Pre-register a counter with help text. Safe to call multiple times."""
        with self._lock:
            self._counters.setdefault(name, {})
            self._help[name] = help_text

    def inc(
        self,
        name: str,
        value: int = 1,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Increment the counter identified by (name, labels)."""
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0) + value

    def render(self) -> str:
        """Render all counters as Prometheus text-format bytes-ready output."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                help_text = self._help.get(name)
                if help_text:
                    lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} counter")
                for labels, value in self._counters[name].items():
                    if labels:
                        label_str = ",".join(f'{k}="{v}"' for k, v in labels)
                        lines.append(f"{name}{{{label_str}}} {value}")
                    else:
                        lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"
