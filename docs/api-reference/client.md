# Client

## TusClient

Main client class for uploading files via TUS protocol.

```python
from resumable_upload import TusClient
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | str | — | TUS server base URL |
| `chunk_size` | int \| float | `1_048_576` (1 MB) | Upload chunk size in bytes |
| `checksum` | bool \| str | `True` | `True` enables SHA1; pass an algorithm name (`"sha1"`, `"sha256"`, `"sha512"`, `"md5"`) to choose; `False` disables. Server must advertise the chosen algorithm. |
| `verify_tls_cert` | bool | `True` | Verify TLS certificates |
| `metadata_encoding` | str | `"utf-8"` | Encoding for metadata values |
| `store_url` | bool | `False` | Persist upload URLs for cross-session resume |
| `url_storage` | URLStorage | `None` | Custom URL storage backend (auto-created as `FileURLStorage()` when `store_url=True` and unset) |
| `fingerprinter` | Fingerprint | `None` | Custom fingerprint implementation (`Fingerprint`, `PartialMD5Fingerprint`, `CallableFingerprint`, or your own) |
| `headers` | dict | `{}` | Custom headers added to all requests |
| `max_retries` | int | `3` | Max retry attempts per chunk (0 = disabled) |
| `retry_delay` | float | `1.0` | Base delay between retries (exponential backoff, capped at 60s) |
| `timeout` | float | `30.0` | Per-request socket timeout in seconds |
| `before_request` | Callable | `None` | Observability hook: `(method, url, headers) -> None`, called before every HTTP request |
| `after_response` | Callable | `None` | Observability hook: `(method, url, status) -> None`, called after every HTTP response |
| `on_should_retry` | Callable | `None` | `(exception, attempt) -> bool`. Return `False` to abort retry; `True` to keep retrying with the standard backoff. |
| `override_patch_method` | bool | `False` | Send PATCH as POST + `X-HTTP-Method-Override: PATCH` for proxies/firewalls that reject PATCH |
| `add_request_id` | bool | `False` | Add a unique `X-Request-ID` UUID to every request for log correlation (a user-supplied header wins) |
| `on_upload_url_available` | Callable | `None` | `(url) -> None`, called as soon as the upload URL is known — right after creation or when resolved from URL storage |

`upload_file()` additionally accepts `metadata_for_partial_uploads` — metadata
attached to each partial created by `parallel_uploads=N` (the final upload
still carries the real `metadata`).

All four options exist on `AsyncTusClient` too.

### Methods

#### `upload_file`

```python
client.upload_file(
    file_path=None,
    file_stream=None,
    metadata={},
    progress_callback=None,
    stop_at=None,
    parallel_uploads=1,
) -> str
```

Upload a file. Returns the upload URL.

- `stop_at` (int): Stop upload at this byte offset (for partial uploads). Clamped to file size automatically.
- `parallel_uploads` (int): When `> 1`, splits the file into N byte ranges, uploads each as a TUS partial upload concurrently, and merges them server-side via the `concatenation` extension. Requires `file_path` (streams cannot be split) and is incompatible with `stop_at`. The server must support `concatenation`.

#### `resume_upload`

```python
client.resume_upload(file_path=None, upload_url="", file_stream=None, progress_callback=None) -> str
```

Resume an interrupted upload from its current server offset.

#### `find_previous_uploads`

```python
client.find_previous_uploads(file_path=None, file_stream=None) -> list[dict]
# Returns: [{"fingerprint": str, "upload_url": str}] (empty if no match)
```

Looks up a resumable upload for the given file by fingerprint. Empty list when `store_url` is off, no URL storage is attached, or no entry matches. Analogous to tus-js-client's `findPreviousUploads`.

#### `create_partial_upload`

```python
client.create_partial_upload(file_path=None, file_stream=None, metadata={}, progress_callback=None) -> str
```

Create and fully upload one partial upload (TUS `concatenation` extension). Pair multiple of these with `create_final_upload()` to merge server-side.

#### `create_final_upload`

```python
client.create_final_upload(partial_urls: list[str], metadata={}) -> str
```

Create a final upload that concatenates the given partial upload URLs (in order) into a single completed upload. All partials must already be fully uploaded; the server returns `400` if any are incomplete.

#### `create_deferred_upload`

```python
client.create_deferred_upload(metadata={}) -> str
```

Create an upload without declaring its length up front (`Upload-Defer-Length` extension). The length is committed on the first PATCH that includes an `Upload-Length` header. Useful when streaming data whose total size is unknown at creation time.

#### `delete_upload`

```python
client.delete_upload(upload_url: str) -> None
```

#### `get_upload_info`

```python
client.get_upload_info(upload_url: str) -> dict
# Returns: {"offset": int, "length": int, "complete": bool, "metadata": dict}
```

#### `get_server_info`

```python
client.get_server_info() -> dict
# Returns: {"version": str, "extensions": list[str], "max_size": int | None}
```

#### `create_uploader`

```python
client.create_uploader(
    file_path=None, file_stream=None, upload_url=None,
    metadata={}, chunk_size=None,
) -> Uploader
```

Create an `Uploader` instance for fine-grained chunk-level control.

### Observability hooks

```python
def before(method, url, headers):
    print(f"-> {method} {url}")


def after(method, url, status):
    print(f"<- {method} {url} {status}")


def should_retry(err, attempt):
    # don't retry permission errors; do retry everything else
    return not isinstance(err, PermissionError)


client = TusClient(
    "http://localhost:8080/files",
    before_request=before,
    after_response=after,
    on_should_retry=should_retry,
)
```

`on_should_retry` is consulted before every retry attempt — return `False` to fail fast on application-level errors that won't recover.

---

## Uploader

Low-level upload controller for chunk-by-chunk control.

```python
from resumable_upload.client.uploader import Uploader
```

Typically obtained via `TusClient.create_uploader()`.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | str | — | Existing upload URL on the server |
| `file_path` | str | `None` | Path to file (required if no `file_stream`) |
| `file_stream` | IO | `None` | File-like object (alternative to `file_path`) |
| `chunk_size` | int | `1_048_576` | Chunk size in bytes |
| `checksum` | bool \| str | `True` | `True` = SHA1; pass an algorithm name to choose; `False` to disable |
| `max_retries` | int | `0` | Retry attempts per chunk |
| `retry_delay` | float | `1.0` | Base retry delay in seconds |
| `timeout` | float | `30.0` | Per-request timeout in seconds |
| `stop_event` | threading.Event | `None` | When set, interrupts retry wait and raises `TusUploadFailed`. Useful for cancellation in threaded applications. |
| `before_request` / `after_response` / `on_should_retry` | Callable | `None` | Same hooks as `TusClient`; forwarded automatically when the uploader is created via `TusClient.create_uploader()`. |

### 409 Handling

When the server returns `409 Conflict` (offset mismatch), the uploader automatically:
1. Sends a HEAD request to retrieve the current server offset
2. Seeks to that offset in the local file
3. Retries the chunk from the correct position

### Methods

| Method | Description |
|--------|-------------|
| `upload()` | Upload entire remaining file |
| `upload_chunk()` | Upload one chunk; returns `True` if more remain |
| `close()` | Release file handle |
| `is_complete` | Property: `True` if offset ≥ file size |
| `stats` | Property: `UploadStats` snapshot |

---

## Async client

`AsyncTusClient` is a fully async counterpart to `TusClient`, backed by
[httpx](https://www.python-httpx.org/) instead of `urllib`. Every public method
is a coroutine; otherwise the API is identical to the sync client.

### Installation

```bash
pip install "resumable-upload[async]"
```

This installs `httpx` alongside the core library. The async classes are not
importable without it — a helpful `ImportError` is raised if you forget.

### Quick start

```python
import asyncio
from resumable_upload import AsyncTusClient


async def main():
    async with AsyncTusClient("http://localhost:8080/files") as client:
        url = await client.upload_file("large.bin", progress_callback=print)
        print("Uploaded to", url)


asyncio.run(main())
```

### AsyncTusClient

```python
from resumable_upload import AsyncTusClient
```

Accepts the same constructor parameters as `TusClient`. Use as an async context
manager (`async with`) so the underlying `httpx.AsyncClient` is properly closed.

#### Awaitable methods

Every sync method on `TusClient` has a direct async equivalent:

| Async method | Description |
|---|---|
| `await client.upload_file(file_path, ...)` | Upload a file; returns the upload URL |
| `await client.resume_upload(file_path, upload_url, ...)` | Resume from current server offset |
| `await client.delete_upload(upload_url)` | Send `DELETE` request |
| `await client.get_upload_info(upload_url)` | `{"offset", "length", "complete", "metadata"}` |
| `await client.get_metadata(upload_url)` | Decoded metadata dict |
| `await client.get_server_info()` | `{"version", "extensions", "max_size"}` |
| `await client.create_partial_upload(file_path, ...)` | Partial upload (concatenation ext.) |
| `await client.create_final_upload(partial_urls, metadata)` | Merge partials server-side |
| `await client.create_deferred_upload(metadata)` | Deferred-length upload |
| `await client.create_uploader(file_path, ...) ` | Returns an `AsyncUploader` |

`parallel_uploads=N` on `upload_file` works the same as the sync client:
the file is split into N byte ranges and uploaded concurrently via `asyncio.gather`.

`find_previous_uploads` stays synchronous — it is a pure local fingerprint
lookup with no I/O, so there is no async variant.

#### Client lifetime

Inside `async with`, the httpx client lives for the block. Outside it, each
call opens the client and closes it again on the way out — except
`create_uploader`, which hands the live client to the `AsyncUploader`. From
that point the client stays open and you own it:

```python
client = AsyncTusClient(url)
uploader = await client.create_uploader("large_file.bin")
await uploader.upload()
await client.aclose()          # your job once you have borrowed the client
```

### AsyncUploader

```python
from resumable_upload import AsyncUploader
```

Typically obtained via `AsyncTusClient.create_uploader()`. Mirrors `Uploader`
with async entry points:

| Method | Description |
|---|---|
| `await uploader.upload()` | Upload entire remaining file |
| `await uploader.upload_chunk()` | Upload one chunk; returns `True` if more remain |
| `uploader.close()` | Release file handle |
| `uploader.is_complete` | Property: `True` if offset ≥ file size |
| `uploader.stats` | Property: `UploadStats` snapshot |

Because `__init__` cannot `await`, use the `AsyncUploader.open()` async
class-method factory when you need to instantiate one directly:

```python
uploader = await AsyncUploader.open(
    url=upload_url,
    file_path="large.bin",
    chunk_size=4 * 1024 * 1024,
)
await uploader.upload()
```

### Example

See `examples/client/async_upload.py` for a self-contained script that
uploads a file with progress reporting, parallel chunks, and cross-session
resume.
