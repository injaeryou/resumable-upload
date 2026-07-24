# Retry & Error Handling

## Automatic Retry

`TusClient` retries failed chunks automatically with exponential backoff.

```python
from resumable_upload import TusClient

client = TusClient(
    "http://localhost:8080/files",
    max_retries=3,  # Retry up to 3 times per chunk (default: 3)
    retry_delay=1.0,  # Base delay; doubles each attempt, capped at 60s
)
```

Backoff schedule for `retry_delay=1.0`: 1s → 2s → 4s → … (max 60s)

To disable retry entirely, set `max_retries=0`.

## Progress Tracking

Pass a callback to `upload_file()` to receive `UploadStats` after each chunk:

```python
from resumable_upload import TusClient, UploadStats


def progress_callback(stats: UploadStats):
    print(
        f"Progress: {stats.progress_percent:.1f}% | "
        f"Speed: {stats.upload_speed / 1024 / 1024:.2f} MB/s | "
        f"ETA: {stats.eta_seconds:.0f}s | "
        f"Chunks: {stats.chunks_completed}/{stats.total_chunks} | "
        f"Retried: {stats.chunks_retried}"
    )


client = TusClient(
    "http://localhost:8080/files",
    chunk_size=1.5 * 1024 * 1024,
    checksum=True,
)
upload_url = client.upload_file(
    "large_file.bin",
    metadata={"filename": "large_file.bin"},
    progress_callback=progress_callback,
)
```

## Exception Handling

```python
from resumable_upload.exceptions import TusCommunicationError, TusUploadFailed

try:
    upload_url = client.upload_file("file.bin")
except TusUploadFailed as e:
    # Chunk failed after all retry attempts, or cancelled via stop_event
    print(f"Upload failed: {e.message}, status: {e.status_code}")
except TusCommunicationError as e:
    # Network/HTTP error during create, HEAD, DELETE, or server info
    print(f"Communication error: {e.message}, status: {e.status_code}")
```

See [Exceptions](../api-reference/exceptions.md) for the full exception reference.
