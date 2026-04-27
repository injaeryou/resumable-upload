# Server

## TusServer

TUS 1.0.0 protocol server implementation.

```python
from resumable_upload import TusServer
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `storage` | Storage | `SQLiteStorage()` | Storage backend |
| `base_path` | str | `"/files"` | Base URL path for uploads |
| `max_size` | int | `0` | Max upload size in bytes (0 = unlimited) |
| `max_chunk_size` | int | `0` | Max individual PATCH body size in bytes (0 = unlimited). Bigger chunks return `413` |
| `upload_expiry` | int | `None` | Upload TTL in seconds (None = no expiry) |
| `cors_allow_origins` | str | `None` | CORS `Access-Control-Allow-Origin` value |
| `cleanup_interval` | int | `60` | Min seconds between expired-upload cleanup runs |
| `request_timeout` | int | `30` | Socket read timeout in seconds for `TusHTTPRequestHandler`. Guards against Slowloris attacks. Set to `0` to disable. |
| `on_incoming_request` | Callable | `None` | Hook called before processing any request |
| `on_upload_create` | Callable | `None` | Hook called before creating an upload |
| `on_upload_complete` | Callable | `None` | Hook called after an upload is fully completed (also fired for final concatenated uploads, never for individual partials) |
| `on_upload_terminate` | Callable | `None` | Hook called after an upload is deleted |
| `metrics_registry` | MetricsRegistry | `None` | Enable Prometheus-text metrics (`/metrics` by default). See [Metrics](../operations/metrics.md). |
| `metrics_path` | str | `"/metrics"` | Path to expose metrics on |
| `lock_backend` | LockBackend | `None` | Distributed lock for PATCH / DELETE write paths. See [Locks](../operations/locks.md). |
| `lock_ttl_seconds` | float | `60.0` | TTL applied when acquiring a lock (released sooner if the request finishes; expired automatically if the holder crashes) |
| `lock_wait_seconds` | float | `5.0` | How long to wait for a contended lock before returning `423 Locked` |
| `checksum_algorithms` | tuple[str, …] | `("sha1",)` | Algorithms to advertise via `Tus-Checksum-Algorithm` and accept on `Upload-Checksum`. Allowed: `sha1`, `sha256`, `sha512`, `md5`. |

### Supported Extensions

`TusServer.SUPPORTED_EXTENSIONS` advertises:

`creation`, `creation-with-upload`, `creation-defer-length`, `termination`, `checksum`, `expiration`, `concatenation`

### Security Defaults

- **Metadata size limit**: `Upload-Metadata` headers larger than 4 KB return `400` (DoS protection)
- **Invalid base64 metadata**: Returns `400` instead of storing raw value
- **Negative `Content-Length`**: Returns `400`
- **Socket timeout**: 30s default via `TusHTTPRequestHandler.setup()` — prevents slow-read attacks
- **Concurrent writes**: Atomic `UPDATE ... WHERE offset = ?` prevents lost updates; returns `409` on conflict
- **`X-HTTP-Method-Override`**: POST may rewrite to PATCH / DELETE / HEAD for environments that block those methods. Validated against the same rules as the underlying method.

### Hooks

Hooks let you intercept requests and react to upload lifecycle events.

| Hook | Timing | Signature | Failure |
|------|--------|-----------|---------|
| `on_incoming_request` | Before any processing | `(method, path, headers) -> None` | Raise `TusHookError` to reject |
| `on_upload_create` | Before upload creation | `(upload_id, metadata, upload_length) -> Optional[dict]` | Return dict to replace metadata. Raise `TusHookError` to reject |
| `on_upload_complete` | After final PATCH completes (or final concatenation merges) | `(upload_id, metadata, file_info) -> None` | Exceptions logged, response unaffected |
| `on_upload_terminate` | After DELETE succeeds | `(upload_id,) -> None` | Exceptions logged, response unaffected |

**Pre-hooks** (`on_incoming_request`, `on_upload_create`) can reject requests by raising `TusHookError(status_code, body)`. Any other exception returns `500`.

**Post-hooks** (`on_upload_complete`, `on_upload_terminate`) never affect the client response — exceptions are caught and logged.

```python
from resumable_upload import TusServer, SQLiteStorage
from resumable_upload.exceptions import TusHookError

def auth_check(method, path, headers):
    if "authorization" not in headers:
        raise TusHookError("Unauthorized", status_code=401)

def on_complete(upload_id, metadata, file_info):
    print(f"Upload {upload_id} completed: {file_info}")

server = TusServer(
    storage=SQLiteStorage(),
    on_incoming_request=auth_check,
    on_upload_complete=on_complete,
)
```

### Multi-algorithm Checksums

The server advertises every enabled algorithm in the OPTIONS response and validates `Upload-Checksum` against the algorithm name the client sends:

```python
TusServer(storage=..., checksum_algorithms=("sha1", "sha256"))
```

Requests using algorithms not in the enabled set return `400`. The client picks which one to send via its `checksum=` parameter.

### Methods

```python
server.handle_request(method, path, headers, body) -> (status, headers, body)
```

Framework-agnostic request handler. See [Web Frameworks](../web-frameworks/flask.md) for Flask / FastAPI / Django adapters, or [`TusASGIApp`](../web-frameworks/asgi.md) for a generic ASGI mount.

---

## TusHTTPRequestHandler

`BaseHTTPRequestHandler` subclass for use with Python's built-in `HTTPServer`.

```python
from resumable_upload import TusHTTPRequestHandler

class Handler(TusHTTPRequestHandler):
    pass

Handler.tus_server = tus_server
server = HTTPServer(("0.0.0.0", 8080), Handler)
```

The socket read timeout is automatically applied via `setup()` using `tus_server.request_timeout`.
