# Concatenation & Parallel Uploads

The TUS [`concatenation` extension](https://tus.io/protocols/resumable-upload.html#concatenation) lets a single logical file be split into N partial uploads that the server merges into one final upload. The library implements both the server side (SQLite, S3, GCS, Azure) and the client side.

## Parallel Upload (high-level)

For large files over high-bandwidth links, ask the client to split the file into `N` byte ranges, upload them concurrently, and merge them server-side:

```python
from resumable_upload import TusClient

client = TusClient("http://localhost:8080/files", chunk_size=1 * 1024 * 1024)
url = client.upload_file("large.bin", parallel_uploads=4)
```

Constraints:

- Requires `file_path` (streams cannot be split deterministically).
- Incompatible with `stop_at`.
- The server must advertise the `concatenation` extension (this library does).

The wire interaction is identical to [`tus-js-client`'s](https://github.com/tus/tus-js-client) `parallelUploads` option, so the same servers work for both clients.

## Manual partial / final control

For workflows that span devices or sessions — e.g., "upload these two halves separately, merge them tonight" — drive the primitives directly:

```python
url1 = client.create_partial_upload("part1.bin")
url2 = client.create_partial_upload("part2.bin")

final_url = client.create_final_upload(
    partial_urls=[url1, url2],
    metadata={"filename": "merged.bin"},
)
```

`create_partial_upload()` performs a complete upload of the given file and tags it `Upload-Concat: partial`. `create_final_upload()` issues a POST with `Upload-Concat: final;<url1> <url2> ...`; the server validates that every partial is fully uploaded and merges them in order.

## Server-side semantics

- Partials carry an `is_partial` flag in storage and are never delivered to `on_upload_complete` individually — only the merged final upload fires the hook.
- `Upload-Expires` is applied to the final concatenated upload too, so the expiration extension covers merged objects just like ordinary uploads.
- All four bundled storage backends (SQLite, S3, GCS, Azure) implement `concatenate_uploads(...)` using their native primitives:
    - SQLite: streamed file-system stitch
    - S3: `UploadPartCopy`
    - GCS: `compose()`
    - Azure: block-list reuse

## Unfinished concatenation (`concatenation-unfinished`)

On backends that support it (SQLite does), a final upload may be POSTed **while
its partials are still uploading**:

- `POST` with `Upload-Concat: final;<urls>` over incomplete partials returns
  `201` with a `Location` but **no** `Upload-Offset` / `Upload-Length` — the
  final is *pending*.
- `HEAD` on a pending final echoes `Upload-Concat: final;…` and omits both
  length headers until assembly completes (per spec).
- The `PATCH` that completes the **last** partial assembles the final
  atomically (concurrent completions assemble exactly once) and fires
  `on_upload_complete` for the final at that moment.
- `PATCH` directly on a pending final returns `403`; `DELETE` works and leaves
  the partials untouched.
- `Tus-Max-Size` is enforced both at final creation (declared lengths) and at
  assembly time (covers deferred-length partials).

The extension token `concatenation-unfinished` is advertised in
`Tus-Extension` only when the storage backend sets
`supports_unfinished_concat = True`. Cloud backends keep the strict behavior
(`400` on incomplete partials) for now.

## Examples

- `examples/client/parallel_upload.py` — `parallel_uploads=N` end-to-end.
