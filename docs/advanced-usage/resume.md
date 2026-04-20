# Resume & Partial Uploads

## Resume an Interrupted Upload

If an upload is interrupted mid-session, resume it by passing the upload URL:

```python
upload_url = client.resume_upload("large_file.bin", upload_url)
```

The client sends a HEAD request to get the current server offset and continues from there.

## Cross-Session Resumability

Persist the upload URL to disk so uploads survive process restarts:

```python
from resumable_upload import TusClient, FileURLStorage

storage = FileURLStorage(".tus_urls.json")
client = TusClient(
    "http://localhost:8080/files",
    store_url=True,
    url_storage=storage,
)

# First run: starts uploading and saves URL keyed by file fingerprint.
# Subsequent runs with the same file: resumes from where it stopped.
upload_url = client.upload_file("large_file.bin")
```

The fingerprint is a SHA-256 hash of the full file content + size, so different files never collide. See `examples/client/resume.py` for a runnable demo.

## Using File Streams

Upload from a file-like object instead of a path:

```python
with open("file.bin", "rb") as fs:
    upload_url = client.upload_file(
        file_stream=fs,
        metadata={"filename": "file.bin"},
    )
```

Streams are supported everywhere `file_path` is accepted (`upload_file`, `resume_upload`, `create_uploader`).

## Partial Uploads (`stop_at`)

Stop uploading at a specific byte offset — useful for testing resume behaviour:

```python
# Upload only the first 1 MB, then stop
upload_url = client.upload_file("large_file.bin", stop_at=1_048_576)

# Later, resume from where it stopped
client.resume_upload("large_file.bin", upload_url)
```

`stop_at` is clamped to the file size automatically, so passing a value larger than the file is safe.
