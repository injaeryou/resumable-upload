# Distributed Locks

Single-process `SQLiteStorage` serializes writes via `threading.Lock` and `fcntl.flock`. For multi-instance deployments — Kubernetes replicas, ALB-fronted servers behind shared cloud storage — you need cross-host coordination so concurrent PATCH/DELETE on the same upload can't interleave. `LockBackend` provides that.

## Enabling

```python
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.locks import InMemoryLockBackend

server = TusServer(
    storage=SQLiteStorage(),
    lock_backend=InMemoryLockBackend(),
    lock_ttl_seconds=60.0,   # auto-release if the holder crashes
    lock_wait_seconds=5.0,   # 423 Locked when contention exceeds this
)
```

When `lock_backend` is set, every PATCH and DELETE acquires a key keyed by `upload_id` before performing the write. Contention beyond `lock_wait_seconds` returns `423 Locked` — the standard "try again later" signal.

## Backends

### `InMemoryLockBackend`

The default for single-process deployments. Per-key expiry plus a global mutex. Replaces nothing in `SQLiteStorage`'s `flock`, but extends coverage to write paths that don't go through the file system (e.g., cloud storage backends).

### `RedisLockBackend`

For multi-host deployments. Requires the `[redis]` extra:

```bash
pip install resumable-upload[redis]
```

```python
import redis
from resumable_upload.locks_redis import RedisLockBackend

client = redis.from_url("redis://localhost:6379/0")
server = TusServer(
    storage=...,
    lock_backend=RedisLockBackend(client),
    lock_ttl_seconds=60.0,
    lock_wait_seconds=5.0,
)
```

Implementation: `SET key token NX PX <ttl>` for atomic acquire; a Lua script ("delete only if I'm still the holder") for atomic release. Works against any Redis deployment, including Cluster (the script operates on a single key).

The Redis client is yours to bring — the backend does not configure connection pooling or retry policy. `RedisLockBackend.release()` is idempotent and swallows transient errors, by contract.

## Custom Backends

Subclass `LockBackend` to plug in an alternative coordinator (Zookeeper, etcd, Postgres advisory locks):

```python
from resumable_upload.locks import LockBackend

class MyLockBackend(LockBackend):
    def acquire(self, key: str, ttl_seconds: float, wait_timeout: float = 0.0) -> str | None:
        # Return a non-empty token on success, None on contention/timeout.
        ...

    def release(self, key: str, token: str) -> None:
        # Idempotent: silently succeed on stale tokens or unknown keys.
        ...
```

The `acquire` token is a capability for `release` — the server passes back exactly the value it received. Implementations must enforce `ttl_seconds` so a crashed holder's lock eventually expires without external intervention.
