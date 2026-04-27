# Storage

## SQLiteStorage

SQLite + filesystem storage backend.

```python
from resumable_upload import SQLiteStorage
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `db_path` | str | `"uploads.db"` | SQLite database file path |
| `upload_dir` | str | `"uploads"` | Directory for uploaded file chunks |

### Concurrency

`write_chunk()` is safe under concurrent access:

- **In-process (threads)**: Per-upload `threading.Lock` ensures only one thread writes to a given upload at a time.
- **Cross-process (multi-worker)**: `fcntl.flock(LOCK_EX)` on the file provides POSIX advisory locking. Falls back gracefully on non-POSIX systems (e.g., Windows).

`update_offset_atomic()` uses `UPDATE ... WHERE offset = expected` — if another request already advanced the offset, it returns `False` and the server responds with `409`.

### Concatenation Support

`SQLiteStorage` implements `concatenate_uploads(final_id, partial_ids, metadata, *, expires_at=None)` — the SQL backend stitches partial uploads into a single file on disk, records the final upload row, and propagates `expires_at` (so concatenated uploads still honor the expiration extension).

The cloud backends (`S3Storage`, `GCSStorage`, `AzureBlobStorage`) implement the same contract using the appropriate native primitive (multipart upload-part-copy, GCS `compose`, Azure block list). Partial uploads created by the client carry an `is_partial` flag; the server never fires `on_upload_complete` for partials individually — only for the final merged upload.

### Deferred Length

The default implementation of `set_upload_length(upload_id, upload_length)` raises `NotImplementedError`. `SQLiteStorage` (and the cloud backends) override it to commit the final length when the client sends `Upload-Defer-Length: 1` at creation and follows up with `Upload-Length: N` on the first PATCH.

### Custom Storage Backends

Subclass `Storage` to implement a custom backend:

```python
from resumable_upload.storage import Storage

class MyStorage(Storage):
    def create_upload(self, upload_id, upload_length, metadata, expires_at=None, is_partial=False): ...
    def get_upload(self, upload_id): ...
    def update_offset(self, upload_id, offset): ...
    def delete_upload(self, upload_id): ...
    def write_chunk(self, upload_id, offset, data): ...
    def read_file(self, upload_id): ...
    def get_file_path(self, upload_id): ...
    def get_expired_uploads(self): ...
    def cleanup_expired_uploads(self): ...
    # Optional overrides:
    def update_offset_atomic(self, upload_id, expected_offset, new_offset) -> bool: ...
    def set_upload_length(self, upload_id, upload_length) -> None: ...
    def concatenate_uploads(self, final_id, partial_ids, metadata, *, expires_at=None) -> int: ...
    def complete_upload(self, upload_id) -> bool: ...  # cloud-only finalize
```

---

## S3Storage

AWS S3 backend using multipart uploads. Requires `pip install resumable-upload[s3]`.

```python
from resumable_upload.storage_s3 import S3Storage

storage = S3Storage(
    bucket="my-uploads",
    prefix="tus",           # optional key prefix
    part_size=8*1024*1024,  # 8MB (default), min 5MB enforced
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `bucket` | str | *required* | S3 bucket name |
| `s3_client` | Any | `None` | Pre-configured boto3 S3 client (created from env if None) |
| `prefix` | str | `""` | Key prefix for all objects |
| `part_size` | int | `8MB` | Target part size for multipart uploads (min 5MB enforced) |

### How it Works

Each upload maps to an S3 multipart upload. Chunks are buffered until `part_size` is reached, then flushed as S3 parts. Call `complete_upload(upload_id)` after the upload finishes to assemble the final object.

For small files (all data fits in the buffer), a single `PutObject` is used instead.

Concatenation is implemented via `UploadPartCopy` so partial-to-final merge happens entirely on the S3 side.

```python
storage.complete_upload(upload_id)  # Assembles the final S3 object
data = storage.read_file(upload_id) # Read the completed file
info = storage.get_file_info(upload_id)
# {"upload_id": "...", "bucket": "my-uploads", "key": "tus/abc123"}
```

---

## GCSStorage

Google Cloud Storage backend using compose API. Requires `pip install resumable-upload[gcs]`.

```python
from resumable_upload.storage_gcs import GCSStorage

storage = GCSStorage(
    bucket="my-uploads",
    prefix="tus",
    part_size=8*1024*1024,
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `bucket` | str | *required* | GCS bucket name |
| `gcs_client` | Any | `None` | Pre-configured `google.cloud.storage.Client` (created from env if None) |
| `prefix` | str | `""` | Key prefix for all objects |
| `part_size` | int | `8MB` | Target part size (min 5MB enforced to limit compose objects) |

### How it Works

Chunks are buffered and flushed as individual part blobs. On `complete_upload()`, parts are assembled using GCS `compose()` (handles the 32-object limit via hierarchical composition). For small files, a direct upload is used. Concatenation also uses `compose()`.

```python
storage.complete_upload(upload_id)
info = storage.get_file_info(upload_id)
# {"upload_id": "...", "bucket": "my-uploads", "key": "tus/abc123"}
```

---

## AzureBlobStorage

Azure Blob Storage backend using staged blocks. Requires `pip install resumable-upload[azure]`.

```python
from resumable_upload.storage_azure import AzureBlobStorage

storage = AzureBlobStorage(
    container="my-uploads",
    connection_string="DefaultEndpointsProtocol=https;...",
    prefix="tus",
    part_size=8*1024*1024,
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `container` | str | *required* | Azure Blob container name |
| `connection_string` | str | `None` | Azure Storage connection string (falls back to `AZURE_STORAGE_CONNECTION_STRING` env var) |
| `container_client` | Any | `None` | Pre-configured `ContainerClient` (takes precedence) |
| `prefix` | str | `""` | Key prefix for all blobs |
| `part_size` | int | `8MB` | Target block size (min 5MB enforced) |

### How it Works

Chunks are buffered and staged as Azure blocks via `stage_block()`. On `complete_upload()`, all blocks are committed via `commit_block_list()` to form the final blob. For small files, a direct `upload_blob()` is used. Concatenation reuses the block-list mechanism — partials are committed by referencing their staged blocks in the final blob's block list.

```python
storage.complete_upload(upload_id)
info = storage.get_file_info(upload_id)
# {"upload_id": "...", "container": "my-uploads", "key": "tus/abc123"}
```

---

## URL Storage Backends

All three implement the `URLStorage` ABC (`get_url(fingerprint)`, `set_url(fingerprint, url)`, `remove_url(fingerprint)`).

### FileURLStorage

JSON file-based URL storage for cross-session resumability.

```python
from resumable_upload import FileURLStorage
storage = FileURLStorage(".tus_urls.json")
```

| Parameter | Type | Default |
|-----------|------|---------|
| `storage_path` | str | `".tus_urls.json"` |

- **In-process (threads)**: `threading.Lock` serializes all reads and writes.
- **Cross-process (multi-worker)**: `fcntl.flock(LOCK_SH/LOCK_EX)` provides shared/exclusive POSIX file locks on a companion `.lock` file. Falls back gracefully on non-POSIX systems.
- Writes use `os.replace()` (atomic rename) to prevent torn reads.

### SQLiteURLStorage

SQLite-backed URL storage. Preferred over `FileURLStorage` for multi-process clients on the same host — SQLite's own locks serialize writes without an extra `.lock` file.

```python
from resumable_upload import SQLiteURLStorage
storage = SQLiteURLStorage("tus_urls.db")
```

| Parameter | Type | Default |
|-----------|------|---------|
| `db_path` | str | `"tus_urls.db"` |
| `timeout` | float | `5.0` |

### InMemoryURLStorage

Fast, process-local URL storage. Everything is forgotten when the process exits — useful for tests and short-lived upload sessions.

```python
from resumable_upload import InMemoryURLStorage
storage = InMemoryURLStorage()
```

### Custom URL Storage Backends

```python
from resumable_upload.url_storage import URLStorage

class MyURLStorage(URLStorage):
    def get_url(self, fingerprint: str) -> str | None: ...
    def set_url(self, fingerprint: str, url: str) -> None: ...
    def remove_url(self, fingerprint: str) -> None: ...
```
