# Resumable Upload

A Python implementation of the [TUS resumable upload protocol](https://tus.io/) v1.0.0 — server and client in one package, with zero runtime dependencies for the core path.

## Features

- **Zero Dependencies (core)** — server, client, and SQLite storage use only the Python standard library
- **Server & Client** — full TUS 1.0.0 implementation of both sides
- **Resume Capability** — automatic in-session and cross-session resume
- **Data Integrity** — `Upload-Checksum` extension with `sha1` / `sha256` / `sha512` / `md5` (configurable per server, client picks one)
- **Retry Logic** — exponential backoff with configurable cap and a custom `on_should_retry` hook
- **Progress Tracking** — detailed `UploadStats` callback
- **Web Framework Support** — Flask, FastAPI, Django, plus a generic ASGI adapter (`TusASGIApp`)
- **Command-line Server** — `resumable-upload serve` console script for running a TUS server with no Python boilerplate
- **Concatenation Extension** — server-side merge of partial uploads (SQLite, S3, GCS, Azure); `parallel_uploads=N` on the client
- **Deferred Length** — `Upload-Defer-Length` extension for streams whose total size is unknown at creation
- **Operability** — pluggable Prometheus-text metrics registry and pluggable distributed `LockBackend` (in-memory + Redis)
- **Cloud Storage** — S3, Google Cloud Storage, Azure Blob Storage backends (optional dependencies)
- **Server Hooks** — intercept requests and react to upload lifecycle events
- **Client Hooks** — `before_request`, `after_response`, `on_should_retry` for observability and retry gating
- **Cross-Session Resume** — three URL-storage backends (`FileURLStorage`, `SQLiteURLStorage`, `InMemoryURLStorage`)
- **`X-HTTP-Method-Override`** — POST tunneling of PATCH/DELETE/HEAD for environments that block those methods
- **Python 3.9+** — tested on 3.9 through 3.14

## Installation

=== "uv"

    ```bash
    uv add resumable-upload
    ```

=== "pip"

    ```bash
    pip install resumable-upload
    ```

### Optional extras

```bash
pip install resumable-upload[s3]            # AWS S3 storage backend
pip install resumable-upload[gcs]           # Google Cloud Storage backend
pip install resumable-upload[azure]         # Azure Blob Storage backend
pip install resumable-upload[redis]         # RedisLockBackend
pip install resumable-upload[all-storage]   # All cloud backends
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

Or skip the boilerplate with the bundled CLI:

```bash
resumable-upload serve --host 0.0.0.0 --port 8080 --upload-dir ./uploads
```

See [CLI](operations/cli.md) for all flags.

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
    progress_callback=progress,
)
print(f"Upload complete: {upload_url}")
```

### Parallel uploads (concatenation extension)

```python
client = TusClient("http://localhost:8080/files", chunk_size=1 * 1024 * 1024)
upload_url = client.upload_file("large.bin", parallel_uploads=4)
```

The file is split into four byte ranges, each uploaded as a TUS partial upload, then merged server-side via the `concatenation` extension. See [Concatenation & Parallel Uploads](advanced-usage/concatenation.md).

## TUS Protocol Compliance

Implements TUS v1.0.0 core plus the `creation`, `creation-with-upload`, `creation-defer-length`, `termination`, `checksum`, `expiration`, and `concatenation` extensions. See [Compliance](compliance.md) for the full breakdown.

---

## README

- [English README](https://github.com/injaeryou/resumable-upload/blob/main/README.md)
- [한국어 README](https://github.com/injaeryou/resumable-upload/blob/main/README.ko.md)
