# Examples

Runnable examples for the `resumable-upload` library, split into server- and
client-side demos.

```
examples/
├── server/
│   ├── http_server.py        Built-in http.server
│   ├── flask_app.py          Flask integration
│   ├── fastapi_app.py        FastAPI (thin route wrapper)
│   ├── django_app.py         Django view
│   ├── asgi_app.py           Direct uvicorn serve via TusASGIApp (async dispatch)
│   ├── async_storage.py      Native-async Storage backend over TusASGIApp
│   └── with_metrics.py       Prometheus /metrics + optional Redis lock
└── client/
    ├── basic_upload.py       Upload a file with progress + retry
    ├── resume.py             Cross-session resume via fingerprint
    ├── low_level_uploader.py Fine-grained Uploader control
    ├── parallel_upload.py    parallel_uploads + manual partial/final
    ├── hooks.py              before_request / after_response / on_should_retry
    └── async_upload.py       AsyncTusClient — async upload with progress + resume
```

## Quick Start

```bash
# 1. Start a server (pick one)
python examples/server/http_server.py           # :8080  (zero deps)
python examples/server/flask_app.py             # :5000
python examples/server/fastapi_app.py           # :8000
python examples/server/django_app.py            # :8000
python examples/server/asgi_app.py              # :8000  (via TusASGIApp)
python examples/server/async_storage.py         # :8000  (native-async backend)
python examples/server/with_metrics.py          # :8080  (/metrics exposed)

# 2. Create a test file
dd if=/dev/urandom of=/tmp/test.bin bs=1M count=20

# 3. Upload from a client
python examples/client/basic_upload.py         http://localhost:8080/files /tmp/test.bin
python examples/client/resume.py               http://localhost:8080/files /tmp/test.bin
python examples/client/low_level_uploader.py   http://localhost:8080/files /tmp/test.bin
python examples/client/parallel_upload.py      http://localhost:8080/files /tmp/test.bin 4
python examples/client/hooks.py                http://localhost:8080/files /tmp/test.bin
python examples/client/async_upload.py         http://localhost:8080/files /tmp/test.bin
```

---

## Server Examples

### `server/http_server.py` — Built-in HTTP server

Zero-dependency server using Python's `http.server`.

```bash
python examples/server/http_server.py           # → :8080
python examples/server/http_server.py 9000      # → :9000
```

Features: 100 MB limit · 1 h upload expiry · 5 min cleanup · CORS enabled.

---

### `server/flask_app.py` — Flask

```bash
pip install flask
python examples/server/flask_app.py             # → :5000
```

---

### `server/fastapi_app.py` — FastAPI (thin route wrapper)

Wraps `TusServer.handle_request` in a single FastAPI route. Good when you
only need one TUS endpoint inside a larger FastAPI app.

```bash
pip install fastapi uvicorn
python examples/server/fastapi_app.py           # → :8000  (docs at /docs)
```

---

### `server/asgi_app.py` — FastAPI mount via `TusASGIApp`

Mounts the TUS server as a sub-app so it participates in the ASGI pipeline
directly. Preferred when the TUS endpoint should handle its own
middleware/lifecycle without going through a FastAPI route.

```bash
pip install fastapi uvicorn
python examples/server/asgi_app.py              # → :8000
```

The adapter awaits `TusServer.handle_request_async`. With the default
`SQLiteStorage`, each storage call falls back to a single `asyncio.to_thread`
hop, so the event loop stays free without any async rewrite.

---

### `server/async_storage.py` — Native-async storage backend

A **true-async** `Storage` backend (`AsyncDictStorage`) that overrides the
`*_async` surface so `handle_request_async` awaits real non-blocking I/O end
to end — never the `to_thread` fallback. The in-memory store stands in for a
production `aiofiles` / `aioboto3` / `asyncpg` backend; copy its shape and swap
the `await asyncio.sleep(0)` calls for real awaited I/O.

```bash
pip install uvicorn
python examples/server/async_storage.py         # → :8000
```

`tests/test_storage_async_native.py` pins the contract: it forbids the
`to_thread` fallback (monkeypatched to raise) and still completes a full
upload, proving every awaited I/O hits a native override.

---

### `server/django_app.py` — Django

```bash
pip install django
python examples/server/django_app.py            # → :8000
```

**Integrating into an existing Django project**:

```python
# views.py — copy tus_upload_view from the example

# urls.py
from django.urls import path
from .views import tus_upload_view

urlpatterns = [
    path("files", tus_upload_view, name="tus-create"),
    path("files/<str:upload_id>", tus_upload_view, name="tus-upload"),
]
```

---

### `server/with_metrics.py` — Prometheus metrics + distributed lock

Demonstrates the production knobs:

- `metrics_registry=MetricsRegistry()` + `/metrics` scrape endpoint
- `lock_backend=InMemoryLockBackend()` by default
- Switches to `RedisLockBackend` when `REDIS_URL` env var is set

```bash
python examples/server/with_metrics.py                    # memory lock
REDIS_URL=redis://localhost:6379/0 python examples/server/with_metrics.py
curl http://localhost:8080/metrics                        # Prometheus format
```

---

## Client Examples

### `client/basic_upload.py` — Basic upload

Uploads a file, reports progress, optionally deletes it at the end.

```bash
python examples/client/basic_upload.py <server_url> <file_path> [headers_json]

# With an auth header
python examples/client/basic_upload.py http://localhost:8080/files file.bin \
    '{"Authorization": "Bearer my-token"}'
```

Features: progress bar · MB/s speed · `max_retries=3` · `timeout=30 s` · delete prompt.

---

### `client/resume.py` — Cross-session resume

Uploads survive process restarts. First run uploads from byte 0; subsequent
runs with the same file reuse the stored URL and continue from the last
confirmed offset.

```bash
python examples/client/resume.py <server_url> <file_path>

# Ctrl-C halfway through, then re-run to resume
python examples/client/resume.py http://localhost:8080/files large.bin
```

Fingerprint → URL mapping is persisted to `.tus_resume_urls.json`.

---

### `client/low_level_uploader.py` — Fine-grained control

Covers every `Uploader` entry point: chunk-by-chunk, `upload()`,
`is_complete`, `stop_at`, resume by URL.

```bash
python examples/client/low_level_uploader.py <server_url> <file_path> [upload_url]
```

---

### `client/parallel_upload.py` — Parallel chunks + manual partial/final

Two demonstrations in one script:

- **`parallel_uploads=N`** — the client splits the file into N byte ranges,
  uploads them concurrently, and the server merges them via the
  `concatenation` extension. Matches `tus-js-client`'s `parallelUploads`.
- **Manual partial/final** — upload parts one at a time and stitch them
  together explicitly.

```bash
# Automatic parallel upload (4 concurrent partials, merged server-side)
python examples/client/parallel_upload.py http://localhost:8080/files big.bin 4

# Manual partial/final demo (uses two in-memory temp files)
python examples/client/parallel_upload.py --manual http://localhost:8080/files
```

---

### `client/hooks.py` — Observability + previous-upload discovery

Wires `before_request` / `after_response` / `on_should_retry` callbacks and
uses `SQLiteURLStorage` + `find_previous_uploads()` to resume across runs.

```bash
python examples/client/hooks.py <server_url> <file_path>

# First run uploads fresh; run again to see resume via find_previous_uploads
python examples/client/hooks.py http://localhost:8080/files large.bin
```

---

### `client/async_upload.py` — Async upload with progress and resume

`AsyncTusClient` counterpart to `basic_upload.py`. Demonstrates async/await
usage, parallel chunks, and cross-session resume — all within a single
`async with` block.

```bash
pip install "resumable-upload[async]"
python examples/client/async_upload.py <server_url> <file_path> [parallel_n]

# Upload with 4 parallel chunks
python examples/client/async_upload.py http://localhost:8080/files large.bin 4

# Re-run to resume automatically via stored fingerprint
python examples/client/async_upload.py http://localhost:8080/files large.bin
```

Requires `httpx` (installed via the `[async]` extra).

---

## Common Configuration

All server examples share these defaults (adjust in-file):

| Parameter | Value | Description |
|-----------|-------|-------------|
| `max_size` | 100 MB | Maximum upload size |
| `upload_expiry` | 3600 s | Uploads expire after 1 hour |
| `cleanup_interval` | 300 s | Expired uploads cleaned every 5 min |
| `cors_allow_origins` | `"*"` | CORS — restrict to a specific origin in production |

---

## Production Notes

- **WSGI/ASGI runners**: use `gunicorn` (Flask/Django) or `uvicorn --workers N` (FastAPI).
- **CORS**: replace `"*"` with your real frontend origin.
- **Auth**: use `on_incoming_request` / `on_upload_create` hooks or a reverse-proxy check.
- **Multi-instance**: pair `SQLiteStorage` on shared storage with `RedisLockBackend`, or use `S3Storage` / `GCSStorage` / `AzureBlobStorage` with a cloud-side bucket.
- **Metrics**: scrape `server/with_metrics.py`'s `/metrics` endpoint from Prometheus / Datadog.
- **HTTPS**: terminate TLS in production; `verify_tls_cert=False` on the client is for self-signed certs in dev only.
