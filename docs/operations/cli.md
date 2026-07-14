# CLI

The package installs a `resumable-upload` console script that runs a TUS server with no Python boilerplate. Equivalent invocation: `python -m resumable_upload`.

## `resumable-upload serve`

```bash
resumable-upload serve --host 0.0.0.0 --port 8080 --upload-dir ./uploads
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind host |
| `--port` | `8080` | Bind port |
| `--base-path` | `/files` | URL base path for uploads |
| `--upload-dir` | `./uploads` | Directory for uploaded files |
| `--db-path` | `./uploads.db` | SQLite database path |
| `--enable-downloads` | off | Serve completed uploads via GET (tusd-style download endpoint) |
| `--max-size` | `0` | Max upload size in bytes (0 = unlimited) |
| `--max-chunk-size` | `0` | Max single PATCH size in bytes (0 = unlimited) |
| `--upload-expiry` | unset | Upload expiry in seconds (unset = no expiry) |
| `--cors-origin` | unset | `Access-Control-Allow-Origin` value (unset = no CORS) |
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

The CLI handles `SIGINT` / `SIGTERM` cleanly and shuts the HTTP server down before exiting.
