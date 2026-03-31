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
| `upload_expiry` | int | `None` | Upload TTL in seconds (None = no expiry) |
| `cors_allow_origins` | str | `None` | CORS `Access-Control-Allow-Origin` value |
| `cleanup_interval` | int | `60` | Min seconds between expired-upload cleanup runs |
| `request_timeout` | int | `30` | Socket read timeout in seconds for `TusHTTPRequestHandler`. Guards against Slowloris attacks. Set to `0` to disable. |
| `on_incoming_request` | Callable | `None` | Hook called before processing any request |
| `on_upload_create` | Callable | `None` | Hook called before creating an upload |
| `on_upload_complete` | Callable | `None` | Hook called after an upload is fully completed |
| `on_upload_terminate` | Callable | `None` | Hook called after an upload is deleted |

### Security Defaults

- **Metadata size limit**: `Upload-Metadata` headers larger than 4 KB return `400` (DoS protection)
- **Invalid base64 metadata**: Returns `400` instead of storing raw value
- **Negative `Content-Length`**: Returns `400`
- **Socket timeout**: 30s default via `TusHTTPRequestHandler.setup()` — prevents slow-read attacks
- **Concurrent writes**: Atomic `UPDATE ... WHERE offset = ?` prevents lost updates; returns `409` on conflict

### Hooks

Hooks let you intercept requests and react to upload lifecycle events.

| Hook | Timing | Signature | Failure |
|------|--------|-----------|---------|
| `on_incoming_request` | Before any processing | `(method, path, headers) -> None` | Raise `TusHookError` to reject |
| `on_upload_create` | Before upload creation | `(upload_id, metadata, upload_length) -> Optional[dict]` | Return dict to replace metadata. Raise `TusHookError` to reject |
| `on_upload_complete` | After final PATCH completes | `(upload_id, metadata, file_info) -> None` | Exceptions logged, response unaffected |
| `on_upload_terminate` | After DELETE succeeds | `(upload_id,) -> None` | Exceptions logged, response unaffected |

**Pre-hooks** (`on_incoming_request`, `on_upload_create`) can reject requests by raising `TusHookError(status_code, body)`. Any other exception returns `500`.

**Post-hooks** (`on_upload_complete`, `on_upload_terminate`) never affect the client response — exceptions are caught and logged.

```python
from resumable_upload import TusServer, SQLiteStorage
from resumable_upload.exceptions import TusHookError

def auth_check(method, path, headers):
    if "authorization" not in headers:
        raise TusHookError(401, "Unauthorized")

def on_complete(upload_id, metadata, file_info):
    print(f"Upload {upload_id} completed: {file_info}")

server = TusServer(
    storage=SQLiteStorage(),
    on_incoming_request=auth_check,
    on_upload_complete=on_complete,
)
```

### Methods

```python
server.handle_request(method, path, headers, body) -> (status, headers, body)
```

Framework-agnostic request handler. See [Web Frameworks](../web-frameworks/flask.md).

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
