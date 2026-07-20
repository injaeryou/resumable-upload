# Resumable Upload

[![Python Version](https://img.shields.io/pypi/pyversions/resumable-upload.svg)](https://pypi.org/project/resumable-upload/)
[![PyPI Version](https://img.shields.io/pypi/v/resumable-upload.svg)](https://pypi.org/project/resumable-upload/)
[![PyPI Downloads](https://img.shields.io/pepy/dt/resumable-upload)](https://pepy.tech/projects/resumable-upload)
[![License](https://img.shields.io/pypi/l/resumable-upload.svg)](https://github.com/injaeryou/resumable-upload/blob/main/LICENSE)

**English** | [한국어](README.ko.md)

A Python implementation of the [TUS resumable upload protocol](https://tus.io/) v1.0.0 for server and client, with zero runtime dependencies.

## ✨ Features

- 🚀 **Zero Dependencies**: Built using Python standard library only (no external dependencies for core functionality)
- 📦 **Server & Client**: Complete implementation of both sides
- 🔄 **Resume Capability**: Automatically resume interrupted uploads
- ✅ **Data Integrity**: Per-chunk checksums (`sha1`/`sha256`/`sha512`/`md5`), sent as header or HTTP trailer
- 🔁 **Retry Logic**: Built-in automatic retry with exponential backoff
- 📊 **Progress Tracking**: Detailed upload progress callbacks with stats
- 🌐 **Web Framework Support**: Integration examples for Flask, FastAPI, and Django
- 🐍 **Python 3.9+**: Supports Python 3.9 through 3.14
- 🏪 **Storage Backend**: SQLite-based storage (extensible to other backends)
- 🔐 **TLS Support**: Certificate verification control and mTLS authentication
- 📝 **URL Storage**: Persist upload URLs across sessions
- ⬇️ **Download Endpoint**: Opt-in tusd-style GET serving of completed uploads with safe headers
- 🎯 **TUS Protocol Compliant**: TUS v1.0.0 core plus every extension — creation, creation-with-upload, creation-defer-length, termination, checksum, expiration, concatenation, and concatenation-unfinished

## 📦 Installation

### Using uv (Recommended)

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the package
uv pip install resumable-upload
```

### Using pip

```bash
pip install resumable-upload
```

## 🚀 Quick Start

### Basic Server

```python
from http.server import HTTPServer
from resumable_upload import TusServer, TusHTTPRequestHandler, SQLiteStorage

# Create storage backend
storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")

# Create TUS server
tus_server = TusServer(storage=storage, base_path="/files")

# Create HTTP handler
class Handler(TusHTTPRequestHandler):
    pass

Handler.tus_server = tus_server

# Start server
server = HTTPServer(("0.0.0.0", 8080), Handler)
print("Server running on http://localhost:8080")
server.serve_forever()
```

### Basic Client

```python
from resumable_upload import TusClient

# Create client
client = TusClient("http://localhost:8080/files")

# Upload file with progress callback
from resumable_upload import UploadStats

def progress(stats: UploadStats):
    print(f"Progress: {stats.progress_percent:.1f}% | "
          f"{stats.uploaded_bytes}/{stats.total_bytes} bytes | "
          f"Speed: {stats.upload_speed_mbps:.2f} MB/s")

upload_url = client.upload_file(
    "large_file.bin",
    metadata={"filename": "large_file.bin"},
    progress_callback=progress
)

print(f"Upload complete: {upload_url}")
```

### Async client

```python
# Async client — pip install "resumable-upload[async]"
import asyncio
from resumable_upload import AsyncTusClient

async def main():
    async with AsyncTusClient("http://localhost:8080/files") as client:
        url = await client.upload_file("large_file.bin")

asyncio.run(main())
```

Every sync `TusClient` method has an awaitable equivalent (upload, resume, delete, concatenation, `parallel_uploads=N`, protocol queries).

### Checksum algorithms

Pick any subset of `sha1`, `sha256`, `sha512`, `md5` to advertise and validate:

```python
TusServer(storage=..., checksum_algorithms=("sha1", "sha256"))
```

Client picks which one to send:

```python
TusClient("...", checksum="sha256")
```

### Client hooks and URL storage

Observability + domain-specific retry gating:

```python
def before(method, url, headers): print(f"-> {method} {url}")
def after(method, url, status):   print(f"<- {method} {status}")
def should_retry(err, attempt):   return not isinstance(err, PermissionError)

client = TusClient(
    "...",
    before_request=before,
    after_response=after,
    on_should_retry=should_retry,
)
```

Three URL-storage backends ship (all implement the same `URLStorage` ABC):

- `FileURLStorage` — durable JSON file, multi-process safe via flock
- `SQLiteURLStorage` — durable DB, recommended for multi-process clients
- `InMemoryURLStorage` — fast, non-durable (tests, short sessions)

Look up a resumable upload by file:

```python
previous = client.find_previous_uploads("big.bin")
if previous:
    client.resume_upload("big.bin", previous[0]["upload_url"])
```

### ASGI (FastAPI, Starlette, Quart, etc.)

Mount a TUS server as an ASGI application:

```python
from fastapi import FastAPI
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

app = FastAPI()
tus = TusServer(storage=SQLiteStorage(), base_path="/files")
app.mount("/files", TusASGIApp(tus))
```

The adapter awaits `TusServer.handle_request_async` directly. Storage backends that keep the default `*_async` implementations inherit `asyncio.to_thread`-based wrappers, so the event loop stays free with no rewrite required. Storage backends that override `*_async` with native async I/O run non-blocking end-to-end.

### Command-line Server

Run a TUS server from the shell without writing any Python:

```bash
# Console script (installed via pip/uv)
resumable-upload serve --host 0.0.0.0 --port 8080 --upload-dir ./uploads

# Or via module invocation
python -m resumable_upload serve --port 8080
```

Flags: `--host`, `--port`, `--base-path`, `--upload-dir`, `--db-path`, `--max-size`, `--max-chunk-size`, `--upload-expiry`, `--cors-origin`, `--cors-credentials`, `--cors-max-age`, `--checksum-algorithms`, `--enable-downloads`, `--behind-proxy`, `--location-base-url`, `--disable-termination`, `--disable-concatenation`, `--metrics-path`, `--lock-backend`, `--redis-url`, `--log-level`. Run `resumable-upload serve --help` for details.

### Parallel chunk uploads

For large files over high-bandwidth connections, split the file into N concurrent partial uploads and merge them server-side via the [concatenation extension](https://tus.io/protocols/resumable-upload.html#concatenation):

```python
client = TusClient("http://localhost:8080/files", chunk_size=1024 * 1024)
url = client.upload_file("large.bin", parallel_uploads=4)
```

Requires a server that implements the TUS `concatenation` extension (this library does). Compatible with [`tus-js-client`](https://github.com/tus/tus-js-client)'s `parallelUploads` option.

### Manual partial / final control

For advanced workflows (e.g., resumable uploads split across devices or sessions), use the partial / final primitives directly:

```python
url1 = client.create_partial_upload("part1.bin")
url2 = client.create_partial_upload("part2.bin")
final_url = client.create_final_upload(
    partial_urls=[url1, url2],
    metadata={"filename": "merged.bin"},
)
```

## 🔧 Advanced Usage

For detailed guides see the **[Advanced Usage section on the docs site](https://injaeryou.github.io/resumable-upload/advanced-usage/retry/)**:

- Automatic retry with exponential backoff and `on_should_retry` gating — [Retry & Error Handling](https://injaeryou.github.io/resumable-upload/advanced-usage/retry/)
- Resume interrupted uploads (in-session and cross-session) — [Resume & Partial Uploads](https://injaeryou.github.io/resumable-upload/advanced-usage/resume/)
- Concatenation extension and `parallel_uploads=N` — [Concatenation & Parallel Uploads](https://injaeryou.github.io/resumable-upload/advanced-usage/concatenation/)
- Tracing and retry hooks — [Observability & Retry Gating](https://injaeryou.github.io/resumable-upload/advanced-usage/observability/)
- Low-level chunk control via `Uploader` + cancellation with `stop_event` — [Low-Level Uploader](https://injaeryou.github.io/resumable-upload/advanced-usage/uploader/)
- Web framework integration: [Flask](https://injaeryou.github.io/resumable-upload/web-frameworks/flask/), [FastAPI](https://injaeryou.github.io/resumable-upload/web-frameworks/fastapi/), [Django](https://injaeryou.github.io/resumable-upload/web-frameworks/django/), [generic ASGI](https://injaeryou.github.io/resumable-upload/web-frameworks/asgi/)
- Operations: [CLI](https://injaeryou.github.io/resumable-upload/operations/cli/), [Metrics](https://injaeryou.github.io/resumable-upload/operations/metrics/), [Distributed Locks](https://injaeryou.github.io/resumable-upload/operations/locks/)

## 📚 API Reference

Full API documentation is available on the docs site: [Client](https://injaeryou.github.io/resumable-upload/api-reference/client/), [Server](https://injaeryou.github.io/resumable-upload/api-reference/server/), [Storage](https://injaeryou.github.io/resumable-upload/api-reference/storage/), [Exceptions & Utilities](https://injaeryou.github.io/resumable-upload/api-reference/exceptions/).

### Quick Reference

| Class | Import | Purpose |
|-------|--------|---------|
| `TusClient` | `from resumable_upload import TusClient` | Upload files via TUS protocol |
| `TusServer` | `from resumable_upload import TusServer` | Serve TUS uploads (framework-agnostic) |
| `TusHTTPRequestHandler` | `from resumable_upload import TusHTTPRequestHandler` | Handler for Python's built-in `HTTPServer` |
| `SQLiteStorage` | `from resumable_upload import SQLiteStorage` | SQLite + filesystem storage backend |
| `FileURLStorage` | `from resumable_upload import FileURLStorage` | JSON file-based URL persistence |
| `Uploader` | `from resumable_upload.client.uploader import Uploader` | Low-level chunk-by-chunk control |

### Key Parameters

**`TusClient`**: `url`, `chunk_size` (default 1 MB), `checksum` (SHA1, default `True`), `max_retries` (default 3), `retry_delay` (default 1.0s, exponential backoff capped at 60s), `timeout` (default 30s), `store_url` / `url_storage` (cross-session resume), `verify_tls_cert`, `headers` — plus request hooks, PATCH-over-POST tunneling, request-ID injection, and more: **[client API reference](https://injaeryou.github.io/resumable-upload/api-reference/client/)**

**`TusServer`**: `storage`, `base_path` (default `/files`), `max_size`, `upload_expiry`, `cors_allow_origins`, `request_timeout` (default 30s — Slowloris protection) — plus proxy/`Location` config, CORS credentials, downloads, checksum trailers, feature toggles, and more: **[server API reference](https://injaeryou.github.io/resumable-upload/api-reference/server/)**

**`SQLiteStorage`**: `db_path` (default `uploads.db`), `upload_dir` (default `uploads`) — thread-safe via per-upload lock; process-safe via `fcntl.flock`

**`FileURLStorage`**: `storage_path` (default `.tus_urls.json`) — thread-safe via `threading.Lock`; process-safe via `fcntl.flock`

## 🔍 TUS Protocol Compliance

This library implements [TUS protocol v1.0.0](https://tus.io/protocols/resumable-upload.html). Full compliance details: **[TUS Compliance](https://injaeryou.github.io/resumable-upload/compliance/)**.

### Extensions

| Extension | Status |
|-----------|--------|
| **core** | ✅ Implemented |
| **creation** | ✅ Implemented |
| **creation-with-upload** | ✅ Implemented |
| **creation-defer-length** | ✅ Implemented |
| **termination** | ✅ Implemented |
| **checksum** | ✅ Implemented (`sha1`/`sha256`/`sha512`/`md5`, header or trailer) |
| **expiration** | ✅ Implemented |
| **concatenation** | ✅ Implemented (SQLite / S3 / GCS / Azure) |
| **concatenation-unfinished** | ✅ Implemented (SQLite) |

> **Note:** the internal client-side fingerprint for cross-session resume uses **SHA-256** and is not part of the TUS protocol. Full header-by-header details: **[compliance matrix](https://injaeryou.github.io/resumable-upload/compliance/)**.

### Non-standard but supported

| Feature | Status |
|---------|--------|
| `X-HTTP-Method-Override` | ✅ Implemented — POST rewrites to PATCH/DELETE/HEAD for environments that block those methods |

## 🧪 Testing

### Using uv (Recommended)

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install all dependencies (dev and test)
make install

# Run minimal tests (excluding web frameworks)
make test-minimal

# Run all tests (including web frameworks)
make test

# Or use Makefile for convenience
make lint              # Run linting
make format            # Format code
make test-minimal      # Run minimal tests
make test              # Run all tests
make test-all-versions # Test on all Python versions (3.9-3.14) - requires tox
make ci                # Run full CI checks (lint + format + test)
make interop           # Cross-implementation interop tests (see below)
```

### Interoperability tests (local, opt-in)

`make interop` verifies real wire compatibility against the TUS ecosystem
(in `tests/test_interop.py`). These run **locally only** — they are not part
of `make ci`, and each pairing skips cleanly when its prerequisite is missing:

| Pairing | Covers | Prerequisite |
|---------|--------|--------------|
| our server ↔ our client | upload, resume, concatenation, download | none (always runs) |
| our client ↔ **tusd** server | upload, concatenation, termination | `tusd` on `PATH` (or `TUSD_BIN=<path>`) |
| our server ↔ **tus-js-client** (Node) | upload, HEAD, download, termination | `node` + `npm install` in `tests/interop/` |
| our server ↔ **tus-py-client** (`tuspy`) | upload, resume, checksum | `uv pip install tuspy` |

`tusd` ships no client binary (server only), so the second reference client
against our server is the tus project's Python client. Each pairing exercises
only the features the counterpart implements — e.g. tusd advertises neither
checksum nor expiration, and tus-py-client exposes no termination call, so
those combinations are intentionally skipped.

## 📖 Documentation

- **Docs site**: [injaeryou.github.io/resumable-upload](https://injaeryou.github.io/resumable-upload/)
- **English README**: [README.md](https://github.com/injaeryou/resumable-upload/blob/main/README.md)
- **한국어 README**: [README.ko.md](https://github.com/injaeryou/resumable-upload/blob/main/README.ko.md)
- **Advanced Usage**: [advanced-usage/retry](https://injaeryou.github.io/resumable-upload/advanced-usage/retry/)
- **Full API Reference**: [api-reference/client](https://injaeryou.github.io/resumable-upload/api-reference/client/)
- **TUS Protocol Compliance**: [compliance](https://injaeryou.github.io/resumable-upload/compliance/)

## 🤝 Contributing

Contributions are welcome! Please check out the [Contributing Guide](.github/CONTRIBUTING.md) for guidelines.

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

This library is inspired by the official [TUS Python client](https://github.com/tus/tus-py-client) and implements the [TUS resumable upload protocol](https://tus.io/).

## 📞 Support

- 📫 Issues: [GitHub Issues](https://github.com/injaeryou/resumable-upload/issues)
- 📖 Documentation: [injaeryou.github.io/resumable-upload](https://injaeryou.github.io/resumable-upload/)
- 🌟 Star us on GitHub!
