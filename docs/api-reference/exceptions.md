# Exceptions & Utilities

## Exceptions

```python
from resumable_upload.exceptions import TusCommunicationError, TusUploadFailed, TusHookError
```

### Client Exceptions

| Exception | Raised when |
|-----------|-------------|
| `TusCommunicationError` | HTTP/network error during create, HEAD, DELETE, or server info requests |
| `TusUploadFailed` | Chunk upload fails after all retry attempts, or upload is cancelled via `stop_event` / `on_should_retry=False` |

`TusUploadFailed` is a subclass of `TusCommunicationError`.

Both exceptions expose:

- `message` (str): Human-readable error description
- `status_code` (int | None): HTTP status code, if available
- `response_content` (bytes | None): Raw response body, if available

### Server Exceptions

| Exception | Raised when |
|-----------|-------------|
| `TusHookError` | Raised in server hooks to reject a request with a specific status code and body |

```python
from resumable_upload.exceptions import TusHookError

raise TusHookError("Upload not allowed", status_code=400)  # status_code defaults to 403
```

- `status_code` (int): HTTP status code to return to the client (default: 403)
- `body` (str): Response body to return to the client (default: `"Forbidden"`)

---

## Fingerprint

File fingerprinting for identifying uploads across sessions. Three strategies ship; all subclass `Fingerprint`.

```python
from resumable_upload import Fingerprint, PartialMD5Fingerprint, CallableFingerprint
```

### `Fingerprint` (default)

```python
fp = Fingerprint()
key = fp.get_fingerprint("large_file.bin")
# e.g. "size:1048576--sha256:a3f5..."
```

SHA-256 of the full file content combined with file size — `size:{bytes}--sha256:{hex}`. Two files with identical first bytes but different content always produce different fingerprints. Slowest but collision-resistant.

### `PartialMD5Fingerprint`

```python
fp = PartialMD5Fingerprint(probe_bytes=64 * 1024)
key = fp.get_fingerprint("large_file.bin")
# e.g. "size:1048576--md5:..."
```

Cheap strategy — MD5 of the first `probe_bytes` bytes plus the file size. Matches tus-py-client's default fingerprint format (`size:<n>--md5:<hex>`), so URL-storage entries can interoperate with that client if you share the same storage file. Higher collision rate than the SHA-256 default; a false match falls back to a server-side `404` at worst.

### `CallableFingerprint`

```python
def my_fp(file_source) -> str:
    return f"user-42:{file_source}"


client = TusClient("...", fingerprinter=CallableFingerprint(my_fp))
```

Adapter that forwards to a user-supplied function. Lets you plug in domain-specific identity schemes (e.g., user id + filename, a CDN URL, a database primary key) without subclassing.

> **Note**: This fingerprint is an internal client-side feature for resumability and is **not part of the TUS protocol**. The TUS `Upload-Checksum` extension separately uses one of `sha1` / `sha256` / `sha512` / `md5` for per-chunk integrity, configured server-side via `checksum_algorithms=`.
