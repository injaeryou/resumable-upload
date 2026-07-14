# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`concatenation-unfinished` extension** (SQLite backend): POST a final
  upload while its partials are still in progress. The final stays *pending*
  (no offset/length) until the last partial completes, then assembles
  atomically and fires `on_upload_complete`. Advertised only when the storage
  backend sets `supports_unfinished_concat`.

- **`checksum-trailer` extension**: `Upload-Checksum` is accepted as an HTTP
  trailer on chunked requests. The bundled `TusHTTPRequestHandler` (and the
  `serve` CLI) parses chunked bodies + trailers; other transports opt in via
  `TusServer(supports_checksum_trailer=True)` after doing the same.

- **Opt-in GET download endpoint** (tusd-style): `TusServer(enable_downloads=True)`
  or `serve --enable-downloads` serves completed uploads over GET. Safe by
  default: always `Content-Disposition: attachment`, sanitized filename,
  validated `Content-Type` from metadata `filetype`; incomplete uploads `404`,
  expired `410`.

- **Client ergonomics** (sync + async): `override_patch_method` (tunnel PATCH
  through POST + `X-HTTP-Method-Override`), `add_request_id` (unique
  `X-Request-ID` per request), `on_upload_url_available` callback, and
  `metadata_for_partial_uploads` for parallel uploads.

- **Expanded server hooks to tusd parity**: `on_chunk_received` (per-chunk
  progress; raise `TusHookError` to stop and delete the upload mid-flight),
  `on_upload_complete` may return `{status_code, headers, body}` to customize
  the finishing response, `on_before_terminate` can veto client DELETEs, and
  `TusServer.terminate_upload()` terminates out-of-band.

### Fixed

- **HEAD on a final upload now echoes `Upload-Concat: final;<urls>`** as the
  spec requires. Source partial ids are persisted on the final upload record
  (`concat_partial_ids`) across all four storage backends; existing SQLite
  databases migrate automatically.

## [0.1.0] - 2026-06-25

Async support on **both** sides of the protocol, with the core install
staying zero-dependency.

### Added

- **Async client** behind the new `[async]` extra (`pip install "resumable-upload[async]"`, httpx):
  `AsyncTusClient` and `AsyncUploader` — a full async counterpart to
  `TusClient` / `Uploader`. Every method has an awaitable equivalent: upload,
  resume, delete, protocol queries (`get_upload_info` / `get_metadata` /
  `get_server_info`), concatenation (`create_partial_upload` /
  `create_final_upload`), `parallel_uploads=N`, deferred-length, checksums,
  retries, and 409 re-sync. `httpx` is imported lazily, so `import
  resumable_upload` never pulls it in.
- **Async server**: `TusServer.handle_request_async`, and `TusASGIApp` now
  awaits it directly. New `Storage.*_async` surface — sync backends inherit
  `asyncio.to_thread` wrappers automatically; true-async backends override
  only the methods they have native implementations for. See
  `examples/server/async_storage.py` for a native-async backend template.
- HEAD on a partial upload now echoes `Upload-Concat: partial`
  (concatenation-extension conformance).
- New runnable examples: `examples/client/async_upload.py` and
  `examples/server/async_storage.py`, both smoke-tested.

### Changed

- Internal refactor: the sync and async paths share one source of truth for
  all protocol rules — server validators (`_plan_*`) and the client's pure
  helpers (`resumable_upload/client/_protocol.py`) — so the two paths cannot
  drift on the wire. No public API change.
- Checksum coverage verified across `sha1` / `sha256` / `sha512` / `md5` on
  both dispatch paths.
- Project URLs updated to the `injaeryou` GitHub owner.
- Deprecated top-level import aliases (`storage_s3` / `storage_gcs` /
  `storage_azure` / `locks_redis` / `client.base`) are now scheduled for
  removal **after 0.1.2** (previously 0.0.8 / 0.1.0).

### Compatibility

- No breaking changes. `httpx` is required only by the `[async]` extra.
- Tested on Python 3.9 – 3.14. Wire-compatible with tus-js-client, tusd,
  uppy, and tus-py-client.

### Known limitations

- HEAD on a **final** concatenated upload does not yet echo
  `Upload-Concat: final;<urls>` (informational only; finals complete on POST,
  so `Upload-Offset` / `Upload-Length` are correct). See `docs/compliance.md`.

[0.1.0]: https://github.com/injaeryou/resumable-upload/releases/tag/v0.1.0
