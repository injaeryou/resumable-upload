# Resumable Upload

A Python implementation of the [TUS resumable upload protocol](https://tus.io/) v1.0.0 — server and client in one package, with zero runtime dependencies.

## Features

- **Zero Dependencies** — built on the Python standard library only
- **Server & Client** — complete implementation of both sides
- **Resume Capability** — automatically resume interrupted uploads
- **Data Integrity** — optional SHA1 per-chunk checksum verification
- **Retry Logic** — exponential backoff with configurable cap
- **Progress Tracking** — detailed `UploadStats` callback
- **Web Framework Support** — Flask, FastAPI, Django integration
- **Python 3.9+** — tested on 3.9 through 3.14
- **SQLite Storage** — built-in backend, extensible to custom backends
- **Cloud Storage** — S3, Google Cloud Storage, Azure Blob Storage backends (optional dependencies)
- **Server Hooks** — intercept requests and react to upload lifecycle events
- **Cross-Session Resume** — persist upload URLs across process restarts

## Installation

=== "uv"

    ```bash
    uv add resumable-upload
    ```

=== "pip"

    ```bash
    pip install resumable-upload
    ```

### Cloud Storage Backends

Install with optional cloud storage dependencies:

```bash
pip install resumable-upload[s3]        # AWS S3
pip install resumable-upload[gcs]       # Google Cloud Storage
pip install resumable-upload[azure]     # Azure Blob Storage
pip install resumable-upload[all-storage]  # All cloud backends
```

## Quick Start

### Basic Server

```python
from http.server import HTTPServer
from resumable_upload import TusServer, TusHTTPRequestHandler, SQLiteStorage

storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")
tus_server = TusServer(storage=storage, base_path="/files")

class Handler(TusHTTPRequestHandler):
    pass

Handler.tus_server = tus_server
server = HTTPServer(("0.0.0.0", 8080), Handler)
print("Server running on http://localhost:8080")
server.serve_forever()
```

### Basic Client

```python
from resumable_upload import TusClient, UploadStats

def progress(stats: UploadStats):
    print(f"Progress: {stats.progress_percent:.1f}% | "
          f"Speed: {stats.upload_speed_mbps:.2f} MB/s")

client = TusClient("http://localhost:8080/files")
upload_url = client.upload_file(
    "large_file.bin",
    metadata={"filename": "large_file.bin"},
    progress_callback=progress
)
print(f"Upload complete: {upload_url}")
```

## TUS Protocol Compliance

Implements TUS v1.0.0 core + creation, creation-with-upload, termination, checksum, and expiration extensions. See [Compliance](compliance.md) for the full breakdown.

---

## README

- [English README](https://github.com/sts07142/resumable-upload/blob/main/README.md)
- [한국어 README](https://github.com/sts07142/resumable-upload/blob/main/README.ko.md)
