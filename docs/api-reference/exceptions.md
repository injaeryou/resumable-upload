# Exceptions & Utilities

## Exceptions

```python
from resumable_upload.exceptions import (
    TusCommunicationError, TusUploadFailed, TusHookError
)
```

### Client Exceptions

| Exception | Raised when |
|-----------|-------------|
| `TusCommunicationError` | HTTP/network error during create, HEAD, DELETE, or server info requests |
| `TusUploadFailed` | Chunk upload fails after all retry attempts, or upload is cancelled via `stop_event` |

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

raise TusHookError(403, "Forbidden")  # status_code defaults to 400
```

- `status_code` (int): HTTP status code to return to the client (default: 400)
- `body` (str): Response body to return to the client

---

## Fingerprint

File fingerprinting for identifying uploads across sessions.

```python
from resumable_upload.fingerprint import Fingerprint

fp = Fingerprint()
key = fp.get_fingerprint("large_file.bin")
# e.g. "size:1048576--sha256:a3f5..."
```

The fingerprint format is `size:{bytes}--sha256:{hex}`. SHA-256 of the full file content is used (not just a header sample), so two files with identical first bytes but different content produce different fingerprints.

> **Note**: This fingerprint is an internal client-side feature for resumability and is **not part of the TUS protocol**. The TUS `Upload-Checksum` extension separately uses SHA1 for per-chunk integrity.
