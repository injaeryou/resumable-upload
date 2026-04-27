# Metrics

`TusServer` ships with a zero-dependency Prometheus-text metrics registry. It exposes counters only — histograms and gauges are deliberately out of scope so the core can stay free of `prometheus_client`.

## Enabling

```python
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.metrics import MetricsRegistry

metrics = MetricsRegistry()
server = TusServer(
    storage=SQLiteStorage(),
    metrics_registry=metrics,
    metrics_path="/metrics",  # default
)
```

When `metrics_registry` is set, the server intercepts GET requests to `metrics_path` and renders the registry as Prometheus text format.

The CLI exposes the same plumbing via `--metrics-path`:

```bash
resumable-upload serve --metrics-path /metrics
```

## Counters

All counters are pre-registered when the server boots:

| Counter | Description |
|---------|-------------|
| `tusd_requests_total` | Total HTTP requests received |
| `tusd_errors_total` | Total responses with status ≥ 400 |
| `tusd_uploads_created_total` | Uploads created (POST succeeded) |
| `tusd_uploads_finished_total` | Uploads completed (final chunk received or final upload merged) |
| `tusd_uploads_terminated_total` | Uploads terminated via DELETE |
| `tusd_bytes_received_total` | Total bytes received across all PATCH requests |

Counter names mirror `tusd`'s for compatibility with existing Grafana dashboards. Use `MetricsRegistry.inc(name, value, labels=...)` to register and increment additional counters from your own hooks.

## Custom integration

`MetricsRegistry` is just a thread-safe in-memory registry — if you already use `prometheus_client` or another collector, leave `metrics_registry=None` and instrument the hooks (`on_upload_complete`, `on_upload_terminate`) yourself.
