"""Production-ish server: Prometheus metrics + optional Redis distributed lock.

Shows how to compose the opt-in observability and concurrency-control
features so operators can scrape ``/metrics`` and safely run multiple
instances behind a load balancer.

Run::

    python examples/server/with_metrics.py                  # memory lock, :8080
    python examples/server/with_metrics.py 9000             # custom port
    REDIS_URL=redis://localhost:6379/0 python examples/server/with_metrics.py

Scrape::

    curl http://localhost:8080/metrics
"""

from __future__ import annotations

import os
import sys
from http.server import HTTPServer

from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.locks import InMemoryLockBackend, LockBackend
from resumable_upload.metrics import MetricsRegistry
from resumable_upload.server import TusHTTPRequestHandler


def _lock_backend_from_env() -> LockBackend:
    """Pick InMemoryLockBackend by default; switch to Redis if REDIS_URL is set."""
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return InMemoryLockBackend()
    import redis  # type: ignore[import-not-found]

    from resumable_upload.locks_redis import RedisLockBackend

    return RedisLockBackend(client=redis.from_url(redis_url))


def build_server(port: int = 8080) -> HTTPServer:
    storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")
    metrics = MetricsRegistry()
    tus = TusServer(
        storage=storage,
        base_path="/files",
        max_size=100 * 1024 * 1024,
        upload_expiry=3600,
        cors_allow_origins="*",
        metrics_registry=metrics,
        metrics_path="/metrics",
        lock_backend=_lock_backend_from_env(),
        lock_ttl_seconds=60.0,
        lock_wait_seconds=5.0,
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus
    return HTTPServer(("0.0.0.0", port), Handler)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = build_server(port)
    print(f"TUS server with metrics+locks on http://localhost:{port}/files")
    print(f"Prometheus scrape at      http://localhost:{port}/metrics")
    print(
        "Lock backend:",
        "Redis" if os.environ.get("REDIS_URL") else "in-memory",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
