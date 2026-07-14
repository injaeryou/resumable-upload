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
| `cors_allow_origins` | str \| list[str] | `None` | CORS origins. A static string (e.g. `"*"`) is emitted as-is; a list is matched against the request `Origin` and echoed back with `Vary: Origin` (no match → no CORS headers) |
| `cors_allow_credentials` | bool | `False` | Emit `Access-Control-Allow-Credentials: true`. A `"*"` origin is then replaced by the echoed request origin (`*` is invalid with credentials) |
| `cors_max_age` | int | `None` | `Access-Control-Max-Age` seconds on preflight (OPTIONS) responses |
| `cleanup_interval` | int | `60` | Min seconds between expired-upload cleanup runs |
| `request_timeout` | int | `30` | Socket read timeout in seconds for `TusHTTPRequestHandler`. Guards against Slowloris attacks. Set to `0` to disable. |
| `on_incoming_request` | Callable | `None` | Hook called before processing any request |
| `on_upload_create` | Callable | `None` | Hook called before creating an upload |
| `on_upload_complete` | Callable | `None` | Hook called after an upload is fully completed (also fired for final concatenated uploads, never for individual partials). May return a dict to customize the finishing response — see Hooks. |
| `on_upload_terminate` | Callable | `None` | Hook called after an upload is deleted |
| `on_chunk_received` | Callable | `None` | Hook called after every accepted PATCH chunk (tusd's post-receive). Raise `TusHookError` to stop and delete the upload. |
| `on_before_terminate` | Callable | `None` | Blocking hook before a client DELETE (tusd's pre-terminate). Raise `TusHookError` to veto. |
| `metrics_registry` | MetricsRegistry | `None` | Enable Prometheus-text metrics (`/metrics` by default). See [Metrics](../operations/metrics.md). |
| `metrics_path` | str | `"/metrics"` | Path to expose metrics on |
| `lock_backend` | LockBackend | `None` | Distributed lock for PATCH / DELETE write paths. See [Locks](../operations/locks.md). |
| `lock_ttl_seconds` | float | `60.0` | TTL applied when acquiring a lock (released sooner if the request finishes; expired automatically if the holder crashes) |
| `lock_wait_seconds` | float | `5.0` | How long to wait for a contended lock before returning `423 Locked` |
| `checksum_algorithms` | tuple[str, …] | `("sha1",)` | Algorithms to advertise via `Tus-Checksum-Algorithm` and accept on `Upload-Checksum`. Allowed: `sha1`, `sha256`, `sha512`, `md5`. |
| `supports_checksum_trailer` | bool | `False` | Advertise `checksum-trailer`. Set only when the transport parses chunked bodies + trailers and merges a trailing `Upload-Checksum` into the headers — the bundled `TusHTTPRequestHandler` / `serve` CLI does. |
| `enable_downloads` | bool | `False` | Serve completed uploads over `GET {base_path}/{id}`. Always `Content-Disposition: attachment` (sanitized filename, RFC 5987 for non-ASCII); `Content-Type` from validated metadata `filetype`, else `application/octet-stream`. Incomplete → `404`, expired → `410`. GET is exempt from the `Tus-Resumable` check. |

### Supported Extensions

`TusServer.SUPPORTED_EXTENSIONS` always advertises:

`creation`, `creation-with-upload`, `creation-defer-length`, `termination`, `checksum`, `expiration`, `concatenation`

Two more are advertised conditionally:

- `concatenation-unfinished` — when the storage backend sets `supports_unfinished_concat` (SQLite does)
- `checksum-trailer` — when constructed with `supports_checksum_trailer=True`

### Security Defaults

- **Metadata size limit**: `Upload-Metadata` headers larger than 4 KB return `400` (DoS protection)
- **Invalid base64 metadata**: Returns `400` instead of storing raw value
- **Negative `Content-Length`**: Returns `400`
- **Socket timeout**: 30s default via `TusHTTPRequestHandler.setup()` — prevents slow-read attacks
- **Concurrent writes**: Atomic `UPDATE ... WHERE offset = ?` prevents lost updates; returns `409` on conflict
- **`X-HTTP-Method-Override`**: POST may rewrite to PATCH / DELETE / HEAD for environments that block those methods. Validated against the same rules as the underlying method.

### Hooks

Hooks let you intercept requests and react to upload lifecycle events.

| Hook | Timing | Signature | Powers |
|------|--------|-----------|--------|
| `on_incoming_request` | Before any processing | `(method, path, headers) -> None` | Raise `TusHookError` to reject |
| `on_upload_create` | Before upload creation | `(upload_id, metadata, upload_length) -> Optional[dict]` | Return dict to replace metadata. Raise `TusHookError` to reject |
| `on_chunk_received` | After every accepted PATCH chunk | `(upload_id, offset, chunk_size) -> None` | Raise `TusHookError` to **stop the upload**: it is deleted and the error status returned (tusd's StopUpload — quota/abuse cutoff). Other exceptions logged and ignored. |
| `on_upload_complete` | After the upload finishes (final PATCH, creation-with-upload, or concat assembly) | `(upload_id, metadata, file_info) -> Optional[dict]` | Return `{"status_code": …, "headers": {…}, "body": …}` to customize the finishing response (tusd's pre-finish), e.g. hand the client a final resource URL. Exceptions logged, response unaffected. |
| `on_before_terminate` | Before a client DELETE is honored | `(upload_id,) -> None` | Raise `TusHookError` to veto the termination (tusd's pre-terminate) |
| `on_upload_terminate` | After DELETE succeeds | `(upload_id,) -> None` | Exceptions logged, response unaffected |

**Pre-hooks** (`on_incoming_request`, `on_upload_create`, `on_before_terminate`) reject by raising `TusHookError(body, status_code=…)`. Any other exception returns `500`.

For out-of-band cancellation there is also `TusServer.terminate_upload(upload_id) -> bool`: deletes the upload and fires `on_upload_terminate`, bypassing the veto hook (the operator calling it has already decided).

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

Framework-agnostic synchronous request handler. See [Web Frameworks](../web-frameworks/flask.md) for Flask / FastAPI / Django adapters.

```python
await server.handle_request_async(method, path, headers, body) -> (status, headers, body)
```

Async sibling used by `TusASGIApp`. Storage backends that keep the default `*_async` implementations delegate to `asyncio.to_thread` automatically; backends that override `*_async` with native async I/O run non-blocking end-to-end. See [`TusASGIApp`](../web-frameworks/asgi.md).

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
