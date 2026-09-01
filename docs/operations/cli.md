# CLI

The package installs a `resumable-upload` console script that runs a TUS server with no Python boilerplate. Equivalent invocation: `python -m resumable_upload`.

## `resumable-upload serve`

```bash
resumable-upload serve --host 0.0.0.0 --port 8080 --upload-dir ./uploads
```

`serve` handles each connection on its own thread, so concurrent `PATCH`es, `--parallel` uploads and lock contention behave the way they will in production. `SIGINT`/`SIGTERM` stop accepting new connections and wait for in-flight requests to finish, bounded by `--request-timeout` (30s default) — set it below your orchestrator's grace period so a stalled client can't push the drain into a `SIGKILL`. Threads are one-per-connection and uncapped, so put the bundled server behind a reverse proxy rather than exposing it directly.

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind host |
| `--port` | `8080` | Bind port |
| `--base-path` | `/files` | URL base path for uploads |
| `--upload-dir` | `./uploads` | Directory for uploaded files |
| `--db-path` | `./uploads.db` | SQLite database path |
| `--enable-downloads` | off | Serve completed uploads via GET (tusd-style download endpoint) |
| `--behind-proxy` | off | Build absolute `Location` URLs from `X-Forwarded-Proto`/`X-Forwarded-Host` (falls back to `Host`, then relative) |
| `--location-base-url` | unset | Fixed absolute prefix for `Location` URLs (e.g. `https://cdn.example`) |
| `--disable-termination` | off | Reject client DELETEs with `405` and drop `termination` from `Tus-Extension` |
| `--disable-concatenation` | off | Reject `Upload-Concat` requests with `400` and drop the concatenation extensions |
| `--max-size` | `0` | Max upload size in bytes (0 = unlimited) |
| `--max-chunk-size` | `0` | Max single PATCH size in bytes (0 = unlimited) |
| `--request-timeout` | `30` | Socket read timeout in seconds; also caps how long a stalled connection delays a graceful shutdown |
| `--upload-expiry` | unset | Upload expiry in seconds (unset = no expiry) |
| `--cors-origin` | unset | `Access-Control-Allow-Origin` value (unset = no CORS) |
| `--cors-credentials` | off | Send `Access-Control-Allow-Credentials`; a `*` origin is echoed per request |
| `--cors-max-age` | unset | `Access-Control-Max-Age` (seconds) on preflight responses |
| `--checksum-algorithms` | `sha1` | Comma-separated `Upload-Checksum` algorithms (e.g. `sha1,sha256,sha512,md5`) |
| `--log-level` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `--metrics-path` | unset | Path to expose Prometheus-text metrics on (unset = disabled) |
| `--lock-backend` | `memory` | One of `none`, `memory`, `redis` |
| `--redis-url` | unset | Redis URL, e.g., `redis://localhost:6379/0` (required when `--lock-backend=redis`) |

### Examples

```bash
# Plain server
resumable-upload serve --port 8080

# With Prometheus metrics and an in-memory lock
resumable-upload serve --metrics-path /metrics

# Multi-instance deployment with Redis-backed locks
pip install resumable-upload[redis]
resumable-upload serve \
  --port 8080 \
  --metrics-path /metrics \
  --lock-backend redis \
  --redis-url redis://redis:6379/0

# Deny gigantic chunks
resumable-upload serve --max-chunk-size $((50 * 1024 * 1024))
```

## Client commands

The same console script also uploads, downloads, and inspects uploads against
any TUS server — no Python needed.

```bash
# Upload (resumable, with a progress line); prints the upload URL
resumable-upload upload big.bin --url http://host/files

# Split into concurrent partials and merge server-side (concatenation)
resumable-upload upload big.bin --url http://host/files --parallel 4

# Attach metadata (repeatable); filename is added automatically
resumable-upload upload report.pdf --url http://host/files --metadata title="Q3"

# Inspect an upload (HEAD): offset, length, complete, metadata
resumable-upload info http://host/files/<id>

# Download a completed upload (needs the server's GET endpoint)
resumable-upload download http://host/files/<id> -o out.bin
```

### `upload` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | required | TUS creation endpoint |
| `--chunk-size` | `4194304` | Chunk size in bytes (4 MB) |
| `--parallel` | `1` | Concurrent partial uploads (concatenation) |
| `--metadata KEY=VALUE` | — | Upload metadata, repeatable |
| `--checksum` | `sha1` | Checksum algorithm, or `none` to disable |
| `--no-progress` | off | Suppress the progress line |

The CLI handles `SIGINT` / `SIGTERM` cleanly and shuts the HTTP server down before exiting.
