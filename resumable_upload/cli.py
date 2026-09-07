"""Command-line entry point for resumable-upload."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from http.server import ThreadingHTTPServer

from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage

log = logging.getLogger("resumable_upload.cli")


class _ThreadingHTTPServer(ThreadingHTTPServer):
    """One thread per connection, joined on close so shutdown drains.

    ``daemon_threads = False`` makes ``server_close()`` block on in-flight
    requests instead of killing them at interpreter exit, so SIGTERM finishes
    the chunk being written rather than truncating it. A stalled connection can
    hold shutdown for up to ``request_timeout`` (30s default), which is the
    socket read timeout that eventually reaps it.
    """

    daemon_threads = False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resumable-upload",
        description="TUS resumable upload server and utilities.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run a TUS server on the given address.")
    serve.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    serve.add_argument(
        "--base-path",
        default="/files",
        help="URL base path for uploads (default: /files)",
    )
    serve.add_argument(
        "--upload-dir",
        default="uploads",
        help="Directory for uploaded files (default: ./uploads)",
    )
    serve.add_argument(
        "--db-path",
        default="uploads.db",
        help="SQLite database path (default: ./uploads.db)",
    )
    serve.add_argument(
        "--enable-downloads",
        action="store_true",
        help="Serve completed uploads via GET (tusd-style download endpoint)",
    )
    serve.add_argument(
        "--behind-proxy",
        action="store_true",
        help="Build absolute Location URLs from X-Forwarded-Proto/Host",
    )
    serve.add_argument(
        "--location-base-url",
        default=None,
        help="Fixed absolute prefix for Location URLs (e.g. https://cdn.example)",
    )
    serve.add_argument(
        "--disable-termination",
        action="store_true",
        help="Reject DELETE requests and drop 'termination' from Tus-Extension",
    )
    serve.add_argument(
        "--disable-concatenation",
        action="store_true",
        help="Reject Upload-Concat requests and drop concatenation extensions",
    )
    serve.add_argument(
        "--max-size",
        type=int,
        default=0,
        help="Max upload size in bytes (0 = unlimited, default: 0)",
    )
    serve.add_argument(
        "--max-chunk-size",
        type=int,
        default=0,
        help="Max chunk size in bytes (0 = unlimited, default: 0)",
    )
    serve.add_argument(
        "--request-timeout",
        type=int,
        default=30,
        help=(
            "Socket read timeout in seconds; also caps how long a stalled "
            "connection can delay a graceful shutdown (default: 30)"
        ),
    )
    serve.add_argument(
        "--upload-expiry",
        type=int,
        default=None,
        help="Upload expiry in seconds (unset = no expiry)",
    )
    serve.add_argument(
        "--cors-origin",
        default=None,
        help="Access-Control-Allow-Origin value (unset = no CORS headers)",
    )
    serve.add_argument(
        "--cors-credentials",
        action="store_true",
        help="Send Access-Control-Allow-Credentials; a '*' origin is echoed back per request",
    )
    serve.add_argument(
        "--cors-max-age",
        type=int,
        default=None,
        help="Access-Control-Max-Age (seconds) on preflight responses",
    )
    serve.add_argument(
        "--checksum-algorithms",
        default="sha1",
        help="Comma-separated Upload-Checksum algorithms to accept "
        "(default: sha1; e.g. sha1,sha256,sha512,md5)",
    )
    serve.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level (default: INFO)",
    )
    serve.add_argument(
        "--metrics-path",
        default=None,
        help="Enable Prometheus metrics at this path (e.g., /metrics). Disabled if unset.",
    )
    serve.add_argument(
        "--lock-backend",
        choices=("none", "memory", "redis"),
        default="memory",
        help="Distributed lock backend for PATCH/DELETE (default: memory). "
        "'redis' requires --redis-url and the [redis] extra.",
    )
    serve.add_argument(
        "--redis-url",
        default=None,
        help="Redis URL (e.g., redis://localhost:6379/0), required when --lock-backend=redis",
    )
    serve.add_argument(
        "--lock-ttl",
        type=float,
        default=60.0,
        help="Lock TTL in seconds; a holder that outruns it can be joined "
        "by a second writer (default: 60). Ignored with --lock-backend=none.",
    )
    serve.add_argument(
        "--lock-wait",
        type=float,
        default=5.0,
        help="How long to wait for a contended lock before returning 423 "
        "(default: 5). Ignored with --lock-backend=none.",
    )
    serve.add_argument(
        "--cleanup-interval",
        type=int,
        default=60,
        help="Minimum seconds between expired-upload cleanup runs (default: 60)",
    )

    # -- client subcommands ------------------------------------------------
    upload = sub.add_parser("upload", help="Upload a file to a TUS server.")
    upload.add_argument("file", help="Path to the file to upload")
    upload.add_argument(
        "--url", required=True, help="TUS creation endpoint, e.g. http://host/files"
    )
    upload.add_argument(
        "--chunk-size", type=int, default=4 * 1024 * 1024, help="Chunk size in bytes (default: 4MB)"
    )
    upload.add_argument(
        "--parallel", type=int, default=1, help="Concurrent partial uploads (default: 1)"
    )
    upload.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Upload metadata (repeatable); filename is added automatically",
    )
    upload.add_argument(
        "--checksum",
        default="sha1",
        help="Checksum algorithm, or 'none' to disable (default: sha1)",
    )
    upload.add_argument("--no-progress", action="store_true", help="Suppress the progress line")

    download = sub.add_parser("download", help="Download a completed upload (server GET endpoint).")
    download.add_argument("url", help="Upload URL to download")
    download.add_argument("-o", "--output", required=True, help="Output file path")

    info = sub.add_parser("info", help="Print offset/length/metadata for an upload (HEAD).")
    info.add_argument("url", help="Upload URL to inspect")

    return parser


def _serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    storage = SQLiteStorage(db_path=args.db_path, upload_dir=args.upload_dir)

    metrics = None
    if args.metrics_path:
        from resumable_upload.metrics import MetricsRegistry

        metrics = MetricsRegistry()

    lock_backend = None
    if args.lock_backend == "memory":
        from resumable_upload.locks import InMemoryLockBackend

        lock_backend = InMemoryLockBackend()
    elif args.lock_backend == "redis":
        if not args.redis_url:
            raise SystemExit("--redis-url is required when --lock-backend=redis")
        import redis

        from resumable_upload.locks.redis_lock import RedisLockBackend

        lock_backend = RedisLockBackend(client=redis.from_url(args.redis_url))

    tus = TusServer(
        storage=storage,
        base_path=args.base_path,
        max_size=args.max_size,
        max_chunk_size=args.max_chunk_size,
        upload_expiry=args.upload_expiry,
        cleanup_interval=args.cleanup_interval,
        request_timeout=args.request_timeout,
        cors_allow_origins=args.cors_origin,
        cors_allow_credentials=args.cors_credentials,
        cors_max_age=args.cors_max_age,
        checksum_algorithms=tuple(
            a.strip() for a in args.checksum_algorithms.split(",") if a.strip()
        ),
        metrics_registry=metrics,
        metrics_path=args.metrics_path or "/metrics",
        lock_backend=lock_backend,
        lock_ttl_seconds=args.lock_ttl,
        lock_wait_seconds=args.lock_wait,
        # The bundled stdlib transport parses chunked bodies + trailers.
        supports_checksum_trailer=True,
        enable_downloads=args.enable_downloads,
        behind_proxy=args.behind_proxy,
        location_base_url=args.location_base_url,
        disable_termination=args.disable_termination,
        disable_concatenation=args.disable_concatenation,
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus

    httpd = _ThreadingHTTPServer((args.host, args.port), Handler)

    def _shutdown(_signum: int, _frame: object) -> None:
        log.info("Shutdown signal received; draining.")
        # shutdown() blocks until serve_forever() returns, and serve_forever()
        # runs on the thread this signal handler interrupts — calling it inline
        # deadlocks the process. Hand it to a thread that can outlive us.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "TUS server listening on http://%s:%s%s (db=%s, dir=%s)",
        args.host,
        args.port,
        args.base_path,
        args.db_path,
        args.upload_dir,
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


def _parse_metadata(pairs: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"--metadata must be KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        metadata[key] = value
    return metadata


def _upload(args: argparse.Namespace) -> int:
    import os

    from resumable_upload.client import TusClient
    from resumable_upload.client.stats import UploadStats

    checksum: bool | str = False if args.checksum.lower() == "none" else args.checksum
    metadata = _parse_metadata(args.metadata)
    metadata.setdefault("filename", os.path.basename(args.file))

    def _progress(stats: UploadStats) -> None:
        pct = (stats.uploaded_bytes / stats.total_bytes * 100) if stats.total_bytes else 100.0
        print(
            f"\r  {pct:5.1f}%  {stats.uploaded_bytes}/{stats.total_bytes} bytes",
            end="",
            flush=True,
        )

    client = TusClient(args.url, chunk_size=args.chunk_size, checksum=checksum)
    url = client.upload_file(
        args.file,
        metadata=metadata,
        parallel_uploads=args.parallel,
        progress_callback=None if args.no_progress else _progress,
    )
    if not args.no_progress:
        print()  # end the progress line
    print(url)
    return 0


def _download(args: argparse.Namespace) -> int:
    import shutil
    import urllib.request

    with urllib.request.urlopen(args.url) as resp, open(args.output, "wb") as out:
        shutil.copyfileobj(resp, out)
    print(f"Saved {args.output}")
    return 0


def _info(args: argparse.Namespace) -> int:
    from resumable_upload.client import TusClient

    client = TusClient(args.url)
    inf = client.get_upload_info(args.url)
    print(f"offset:   {inf['offset']}")
    print(f"length:   {inf['length']}")
    print(f"complete: {inf['complete']}")
    if inf.get("metadata"):
        print("metadata:")
        for k, v in inf["metadata"].items():
            print(f"  {k}: {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {"serve": _serve, "upload": _upload, "download": _download, "info": _info}
    handler = handlers.get(args.command)
    if handler is not None:
        return handler(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
