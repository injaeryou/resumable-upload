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
| **expiration** | ✅ Implemented | `Upload-Expires` in POST / HEAD / PATCH responses (also propagated to final concatenated uploads); periodic server-side cleanup |
| **concatenation** | ✅ Implemented | `Upload-Concat: partial` and `Upload-Concat: final;url1 url2 …`; supported by SQLite, S3, GCS, and Azure backends |

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
| HEAD on a final echoes `Upload-Concat: final;…` | ⚠️ Not implemented | Source partial URLs aren't persisted, so the original `final;…` value isn't reconstructed on HEAD. Finals complete synchronously on POST (`Upload-Offset == Upload-Length`), so the post-concatenation offset/length requirement is met; only the header echo is absent. |
| Concurrent PATCH/DELETE serialized via `LockBackend` | ✅ | Optional; `423 Locked` on contention beyond `lock_wait_seconds` |
| Malformed `Content-Length` header → `400` | ✅ | |
| Negative `Content-Length` → `400` | ✅ | |
| `Upload-Metadata` larger than 4 KB → `400` | ✅ | DoS protection |
| Invalid base64 in `Upload-Metadata` → `400` | ✅ | |
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

## Not Implemented

| Feature | Notes |
|---------|-------|
| Multiple TUS version support | Only `1.0.0` supported |

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
