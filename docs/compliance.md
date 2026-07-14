# TUS Protocol Compliance

Compliance status against the [TUS resumable upload protocol v1.0.0](https://tus.io/protocols/resumable-upload.html).

## Extensions

| Extension | Status | Notes |
|-----------|--------|-------|
| **core** | ✅ Implemented | POST / HEAD / PATCH, offset tracking, version negotiation |
| **creation** | ✅ Implemented | Upload creation via POST with `Upload-Length` |
| **creation-with-upload** | ✅ Implemented | Initial data in POST body (`Content-Type: application/offset+octet-stream`) |
| **creation-defer-length** | ✅ Implemented | `Upload-Defer-Length: 1` on POST; final length committed on the first PATCH via `Upload-Length` |
| **termination** | ✅ Implemented | Upload deletion via DELETE |
| **checksum** | ✅ Implemented | Configurable algorithms (`sha1`, `sha256`, `sha512`, `md5`); server advertises every enabled algorithm in `Tus-Checksum-Algorithm` |
| **checksum-trailer** | ✅ Implemented (bundled transport) | `Upload-Checksum` accepted as an HTTP trailer on chunked requests. Trailer parsing is transport-level: the bundled `TusHTTPRequestHandler` (and the `resumable-upload serve` CLI) does it and merges the trailer into the headers before dispatch. Advertised only when `TusServer(supports_checksum_trailer=True)` — set it if your own transport does the same; ASGI/framework adapters don't yet. |
| **expiration** | ✅ Implemented | `Upload-Expires` in POST / HEAD / PATCH responses (also propagated to final concatenated uploads); periodic server-side cleanup |
| **concatenation** | ✅ Implemented | `Upload-Concat: partial` and `Upload-Concat: final;url1 url2 …`; supported by SQLite, S3, GCS, and Azure backends |
| **concatenation-unfinished** | ✅ Implemented (SQLite) | POST `Upload-Concat: final;…` while partials are still in progress creates a *pending* final (201, no `Upload-Offset`/`Upload-Length`); assembly + `on_upload_complete` fire when the last partial completes. Advertised in `Tus-Extension` only when the storage backend sets `supports_unfinished_concat` (SQLite does; cloud backends not yet). PATCH on a pending final → `403`. |

## Version Negotiation

| Requirement | Status |
|-------------|--------|
| Client sends `Tus-Resumable` on all non-OPTIONS requests | ✅ |
| Server returns `Tus-Resumable` on all responses | ✅ |
| Server returns `412` on version mismatch | ✅ |
| Server skips version check for OPTIONS | ✅ |
| Server advertises supported versions in `Tus-Version` (OPTIONS) | ✅ |

## Core Protocol — Server

| Requirement | Status | Notes |
|-------------|--------|-------|
| POST creates new upload, returns `201` + `Location` | ✅ | |
| POST returns `400` on missing/invalid `Upload-Length` | ✅ | |
| POST returns `400` on negative `Upload-Length` | ✅ | |
| POST returns `413` when upload exceeds `Tus-Max-Size` | ✅ | |
| POST returns `413` when an individual PATCH exceeds `max_chunk_size` | ✅ | Server-only knob |
| HEAD returns `200` with `Upload-Offset` + `Upload-Length` | ✅ | `Upload-Length` omitted for deferred-length uploads until committed |
| HEAD includes `Cache-Control: no-store` | ✅ | |
| HEAD returns `404` for unknown upload | ✅ | |
| PATCH appends data, returns `204` + updated `Upload-Offset` | ✅ | |
| PATCH returns `415` on wrong `Content-Type` | ✅ | Must be `application/offset+octet-stream` |
| PATCH returns `409` on `Upload-Offset` mismatch | ✅ | |
| PATCH returns `400` on negative `Upload-Offset` | ✅ | |
| PATCH returns `400` if chunk would exceed `Upload-Length` | ✅ | |
| PATCH returns `460` on checksum mismatch | ✅ | Non-standard but widely used |
| PATCH returns `400` on unsupported `Upload-Checksum` algorithm | ✅ | Algorithm must be in `checksum_algorithms` |
| PATCH returns `403` on already-completed upload | ✅ | |
| PATCH returns `410` on expired upload | ✅ | |
| OPTIONS returns `204` with server capabilities | ✅ | |
| OPTIONS includes `Tus-Checksum-Algorithm` | ✅ | Lists every enabled algorithm |
| DELETE removes upload, returns `204` | ✅ | |
| DELETE returns `404` for unknown upload | ✅ | |
| `Upload-Concat: partial` creates a partial upload | ✅ | Partials never fire `on_upload_complete` individually |
| HEAD on a partial echoes `Upload-Concat: partial` | ✅ | Lets a conformant client distinguish a partial from a normal upload |
| `Upload-Concat: final;…` merges partials into a final upload | ✅ | Returns `400` if any partial is missing or incomplete |
| HEAD on a final echoes `Upload-Concat: final;…` | ✅ | Source partial IDs are persisted on the final upload (`concat_partial_ids`); HEAD reconstructs `final;<relative urls>` in original order. Supported by SQLite, S3, GCS, and Azure backends. |
| Concurrent PATCH/DELETE serialized via `LockBackend` | ✅ | Optional; `423 Locked` on contention beyond `lock_wait_seconds` |
| Malformed `Content-Length` header → `400` | ✅ | |
| Negative `Content-Length` → `400` | ✅ | |
| `Upload-Metadata` larger than 4 KB → `400` | ✅ | DoS protection |
| Invalid base64 in `Upload-Metadata` → `400` | ✅ | |
| Duplicate `Upload-Metadata` key → `400` | ✅ | Spec: keys MUST be unique within the header |
| Non-ASCII `Upload-Metadata` key → `400` | ✅ | Spec: keys MUST be ASCII. Bare keys (empty value, `SP` optional) accepted |
| Socket read timeout (Slowloris protection) | ✅ | `TusHTTPRequestHandler.setup()` applies `request_timeout` (default 30s) |

## Core Protocol — Client

| Requirement | Status | Notes |
|-------------|--------|-------|
| Sends `Tus-Resumable: 1.0.0` on all requests | ✅ | |
| POST to create upload with `Upload-Length` | ✅ | |
| POST to create deferred-length upload (`Upload-Defer-Length: 1`) | ✅ | `create_deferred_upload()` |
| HEAD to get current offset before resuming | ✅ | |
| PATCH with `Upload-Offset` and correct `Content-Type` | ✅ | |
| `Content-Length: 0` in DELETE request | ✅ | |
| Configurable timeout on all `urlopen()` calls | ✅ | Default 30s |
| Catches `URLError` (network-level) alongside `HTTPError` | ✅ | |
| Exponential backoff with cap (max 60s) | ✅ | |
| Custom retry gating via `on_should_retry` | ✅ | Domain-specific abort |
| `Upload-Checksum` (configurable algorithm) | ✅ | `sha1` default; `sha256`, `sha512`, `md5` opt-in |
| Cross-session URL persistence (fingerprint-based) | ✅ | `FileURLStorage` / `SQLiteURLStorage` / `InMemoryURLStorage` |
| Full-file fingerprint (not just first 64 KB) | ✅ | SHA-256 of entire content (default; `PartialMD5Fingerprint` and `CallableFingerprint` available) |
| `409` on concurrent offset conflict (atomic CAS) | ✅ | `UPDATE ... WHERE offset = ?`; returns `409` if row not updated |
| `409` received → HEAD re-sync before retry | ✅ | Client fetches current offset and re-seeks before retrying chunk |
| Parallel concatenation upload | ✅ | `parallel_uploads=N` on `upload_file()` |
| Manual partial / final concatenation primitives | ✅ | `create_partial_upload()` / `create_final_upload()` |

## Non-standard but supported

| Feature | Notes |
|---------|-------|
| `X-HTTP-Method-Override` | POST rewrites to PATCH/DELETE/HEAD for environments that block those methods |
| `423 Locked` on lock contention | Returned when `lock_backend` is configured and the wait timeout elapses |
| GET download endpoint | tusd-style download of completed uploads. Opt-in: `TusServer(enable_downloads=True)` / `resumable-upload serve --enable-downloads`. Always `Content-Disposition: attachment` (anti-XSS); `Content-Type` from validated metadata `filetype`, else `application/octet-stream`. Incomplete → 404, expired → 410. GET is exempt from the `Tus-Resumable` check (browsers don't send it). |

## Not Implemented

| Feature | Notes |
|---------|-------|
| Multiple TUS version support | Only `1.0.0` supported |
| tus2 / IETF RUFH (`draft-ietf-httpbis-resumable-upload`) | The standards-track successor protocol (not wire-compatible with 1.0.0). Still a moving draft (draft-11, breaking changes between revisions). Planned as an opt-in experimental protocol flag once the draft stabilizes, mirroring tus-js-client's `ietf-draft-NN` approach. |

## Ecosystem Comparison

Feature-by-feature comparison against the most mature official implementations: [tusd](https://github.com/tus/tusd) (Go, the reference server) and [tus-js-client](https://github.com/tus/tus-js-client) (the reference client). Verified against tusd docs/flags and tus-js-client docs/api.md as of 2026-07.

### Server: tusd vs `resumable-upload`

| Area | tusd (official Go server) | resumable-upload |
|------|---------------------------|------------------|
| **TUS extensions advertised** | 5: `creation`, `creation-with-upload`, `creation-defer-length`, `termination`, `concatenation` | 9: those 5 **+ `checksum`, `checksum-trailer`, `expiration`, `concatenation-unfinished`** |
| **Checksum verification** | ❌ none (no `Upload-Checksum` support at all) | ✅ sha1/sha256/sha512/md5, header or trailer, `460` on mismatch |
| **Expiration** | ❌ no TTL/cleanup in the binary (external cleanup required) | ✅ `Upload-Expires` + periodic server-side cleanup, `410` on expired |
| **Concatenation over unfinished partials** | ❌ | ✅ (SQLite backend) |
| **Storage backends** | local disk, S3 (+ S3-compatible endpoint, transfer acceleration, part-size tuning, R2 quirks), GCS, Azure (access tiers) | SQLite (zero-dep default), S3, GCS, Azure via extras; less S3 knob depth (no acceleration/part tuning flags) |
| **Locking** | file locker / in-memory; cooperative lock hand-off; **no distributed locker** (sticky sessions required to scale out) | in-memory / **Redis (distributed)**; `423` on contention; plus atomic offset CAS → `409` on concurrent PATCH |
| **Hooks: mechanisms** | in-process (Go pkg) + **out-of-process: file / HTTP / gRPC / plugin** | in-process Python callables only (embedded-library trade-off) |
| **Hooks: events** | 7: pre-create, post-create, **post-receive (progress, ~1s interval)**, pre-finish, post-finish, **pre-terminate (veto)**, post-terminate | 6: `on_incoming_request` (reject), `on_upload_create` (reject / replace metadata), `on_chunk_received` (per-chunk progress + stop), `on_upload_complete` (custom finishing response), `on_before_terminate` (veto), `on_upload_terminate` |
| **Hooks: powers** | reject create/terminate, **StopUpload mid-flight**, override upload ID & storage path, custom HTTP response (pre-create/pre-finish/post-receive) | reject via `TusHookError(status)`, replace metadata on create, **stop mid-flight** (raise in `on_chunk_received` → upload deleted), custom finishing response (return dict from `on_upload_complete`), out-of-band `terminate_upload()`; custom upload IDs / storage paths still not supported |
| **Download endpoint** | GET, **on by default** (`-disable-download` to off); `filetype` → Content-Type; no Content-Disposition control | GET, **opt-in** (`enable_downloads=True` / `--enable-downloads`); validated Content-Type + always-`attachment` disposition (anti-XSS), sanitized filename |
| **CORS** | on by default: regex origin, credentials, extra allow/expose headers, max-age, `-disable-cors` | opt-in: static string (legacy) or origin list with echo + `Vary: Origin`, `cors_allow_credentials` (wildcard auto-replaced by echoed origin), `cors_max_age` on preflight; no regex origins or extra-header knobs |
| **Metrics** | Prometheus `/metrics` + pprof profiling | Prometheus `/metrics` (zero-dep registry); no pprof |
| **Proxy support** | `-behind-proxy` honors `X-Forwarded-*` / `Forwarded` for absolute Location | relative `Location` only (proxy-safe by construction, but no absolute-URL option, no forwarded-header handling) |
| **Networking / TLS** | UNIX socket, HTTP/2 + h2c, TLS 1.2/1.3 modes, network + request-completion timeouts | stdlib `http.server` (CLI) / any ASGI server; TLS via your reverse proxy or ASGI server; Slowloris socket timeout |
| **Size limits** | `-max-size` | `max_size` **+ per-PATCH `max_chunk_size`** (tusd has no per-chunk cap) |
| **Feature toggles** | `-disable-termination`, `-disable-concatenation`, `-disable-download` | downloads opt-in; no termination/concatenation disable toggles yet |
| **Graceful shutdown** | SIGINT/SIGTERM drain with `-shutdown-timeout` | ❌ (CLI exits immediately) |
| **Structured logging / request IDs** | `-log-format json`, X-Request-ID surfaced in logs | Python `logging` only |
| **tus2 / IETF RUFH** | ✅ experimental (`-enable-experimental-protocol`) | ❌ (tracked; waiting for draft to stabilize) |
| **Deployment model** | standalone binary (also usable as Go package) | embeddable Python library (zero-dep core) + CLI + ASGI adapter + Flask/FastAPI/Django examples |

Summary: ahead of tusd on **protocol surface** (checksum, expiration, unfinished concat) and **distributed locking**; behind on **operational depth** (hook system, proxy/TLS/networking, CORS, graceful shutdown, S3 tuning).

### Client: tus-js-client vs `resumable-upload` client

| Area | tus-js-client (official JS client) | resumable-upload client |
|------|-------------------------------------|-------------------------|
| **Inputs** | File/Blob (browser), Buffer/Readable (node), Cordova file, React Native URI | `file_path` or any file-like `file_stream` (sync); same for httpx-based async client |
| **Chunking** | `chunkSize` default `Infinity` (whole file in one PATCH) | `chunk_size` default 1 MiB |
| **Checksum** | ❌ not implemented (explicitly out of scope per FAQ) | ✅ `Upload-Checksum` per chunk, sha1 default, sha256/sha512/md5 opt-in |
| **Retry** | `retryDelays` array `[0,1s,3s,5s]`, `onShouldRetry` override | `max_retries` + exponential backoff (cap 60s), `on_should_retry` override, `stop_event` interrupts waits |
| **Resume across sessions** | fingerprint → urlStorage (localStorage default **on**), `findPreviousUploads()` / `resumeFromPreviousUpload()` | fingerprint → URL storage (File/SQLite/Memory backends, default **off** via `store_url`), `find_previous_uploads()` / `resume_upload()` |
| **Fingerprint strength** | environment default (name/size-based), pluggable | full-file SHA-256 default (collision-proof, costlier), partial-MD5 and callable alternatives |
| **Parallel upload (concatenation)** | `parallelUploads=N`, custom `parallelUploadBoundaries`, `metadataForPartialUploads` | `parallel_uploads=N` + `metadata_for_partial_uploads`; even split only (custom boundaries deliberately skipped) |
| **creation-with-upload** | ✅ `uploadDataDuringCreation` | ✅ `initial_data` on create |
| **defer-length** | ✅ `uploadLengthDeferred` | ✅ `create_deferred_upload()` |
| **Termination** | ✅ `abort(true)` / static `terminate()` | ✅ `delete_upload()` |
| **Pause / partial stop** | `abort()` (resume later) | `stop_event` (interrupt-safe), `stop_at` byte offset |
| **Request lifecycle hooks** | `onBeforeRequest`, `onAfterResponse`, `onUploadUrlAvailable` | `before_request`, `after_response`, `on_upload_url_available` |
| **Progress reporting** | `onProgress(bytesSent,total)`, `onChunkComplete` | `progress_callback(UploadStats)` incl. speed, ETA, chunks completed |
| **`X-HTTP-Method-Override`** | ✅ `overridePatchMethod` | ✅ `override_patch_method` (sync + async) |
| **Request IDs** | ✅ `addRequestId` (X-Request-ID) | ✅ `add_request_id` (UUID per request; user header wins) |
| **TLS control** | n/a in browser | `verify_tls_cert`, mTLS client certificates |
| **Async** | Promise-based | separate `AsyncTusClient` (httpx, `[async]` extra) |
| **tus2 / IETF RUFH** | ✅ experimental `protocol: 'ietf-draft-03'/'ietf-draft-05'` | ❌ (tracked) |

Summary: ahead on **integrity** (checksum), **fingerprint strength**, TLS/mTLS, and stats-rich progress; behind only on **RUFH experimentation** and custom parallel boundaries.

## Error Response Reference

| Status | Meaning | Trigger |
|--------|---------|---------|
| `400` | Bad Request | Missing/invalid header, negative offset, chunk overflow, oversized metadata, unsupported checksum algorithm, malformed `Upload-Concat`, partial referenced by a final that isn't complete |
| `403` | Forbidden | PATCH on already completed upload |
| `404` | Not Found | Unknown upload ID |
| `409` | Conflict | `Upload-Offset` mismatch or concurrent write conflict |
| `410` | Gone | Upload has expired |
| `412` | Precondition Failed | Unsupported TUS version |
| `413` | Payload Too Large | Exceeds `Tus-Max-Size` or `max_chunk_size` |
| `415` | Unsupported Media Type | Wrong `Content-Type` in PATCH |
| `423` | Locked | `LockBackend` contention; another holder still owns the upload |
| `460` | Checksum Mismatch | Configured-algorithm digest verification failed |

## References

- [TUS Protocol Specification v1.0.0](https://tus.io/protocols/resumable-upload.html)
- [TUS Protocol Extensions](https://tus.io/protocols/resumable-upload.html#protocol-extensions)
