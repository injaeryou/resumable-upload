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

### Custom Storage Backends

Subclass `Storage` to implement a custom backend:

```python
from resumable_upload.storage import Storage

class MyStorage(Storage):
    def create_upload(self, upload_id, upload_length, metadata, expires_at=None): ...
    def get_upload(self, upload_id): ...
    def update_offset(self, upload_id, offset): ...
    def delete_upload(self, upload_id): ...
    def write_chunk(self, upload_id, offset, data): ...
    def read_file(self, upload_id): ...
    def get_file_path(self, upload_id): ...
    def get_expired_uploads(self): ...
    def cleanup_expired_uploads(self): ...
    # Optional override for true atomicity (default: non-atomic read-then-write):
    def update_offset_atomic(self, upload_id, expected_offset, new_offset) -> bool: ...
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

Chunks are buffered and flushed as individual part blobs. On `complete_upload()`, parts are assembled using GCS `compose()` (handles the 32-object limit via hierarchical composition). For small files, a direct upload is used.

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

Chunks are buffered and staged as Azure blocks via `stage_block()`. On `complete_upload()`, all blocks are committed via `commit_block_list()` to form the final blob. For small files, a direct `upload_blob()` is used.

```python
storage.complete_upload(upload_id)
info = storage.get_file_info(upload_id)
# {"upload_id": "...", "container": "my-uploads", "key": "tus/abc123"}
```

---

## FileURLStorage

JSON file-based URL storage for cross-session resumability.

```python
from resumable_upload import FileURLStorage
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `storage_path` | str | `".tus_urls.json"` | Path to the JSON storage file |

### Concurrency

- **In-process (threads)**: `threading.Lock` serializes all reads and writes.
- **Cross-process (multi-worker)**: `fcntl.flock(LOCK_SH/LOCK_EX)` provides shared/exclusive POSIX file locks on a companion `.lock` file. Falls back gracefully on non-POSIX systems.
- Writes use `os.replace()` (atomic rename) to prevent torn reads.

### Custom URL Storage Backends

```python
from resumable_upload.url_storage import URLStorage

class MyURLStorage(URLStorage):
    def get_url(self, fingerprint: str) -> str | None: ...
    def set_url(self, fingerprint: str, url: str) -> None: ...
    def remove_url(self, fingerprint: str) -> None: ...
```
