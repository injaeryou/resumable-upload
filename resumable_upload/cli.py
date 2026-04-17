"""Command-line entry point for resumable-upload."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from http.server import HTTPServer

from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage

log = logging.getLogger("resumable_upload.cli")


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

    tus = TusServer(
        storage=storage,
        base_path=args.base_path,
        max_size=args.max_size,
        max_chunk_size=args.max_chunk_size,
        upload_expiry=args.upload_expiry,
        cors_allow_origins=args.cors_origin,
        metrics_registry=metrics,
        metrics_path=args.metrics_path or "/metrics",
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus

    httpd = HTTPServer((args.host, args.port), Handler)

    def _shutdown(_signum: int, _frame: object) -> None:
        log.info("Shutdown signal received; stopping.")
        httpd.shutdown()

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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
