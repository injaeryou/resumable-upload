# Distributed Locks

Concurrent PATCH/DELETE on the same upload must not interleave — two racing PATCHes at the same offset can otherwise write chunk bytes and advance the offset out of order and corrupt the committed data. `LockBackend` serializes those writes per `upload_id`.

`TusServer` defaults to an in-process `InMemoryLockBackend`, so a server embedded in a threaded or async host (FastAPI, threaded WSGI) is safe **within one process** out of the box. Multi-instance deployments — Kubernetes replicas, ALB-fronted servers behind shared cloud storage — need cross-host coordination and **must** pass a distributed `RedisLockBackend`. (`TusServerCore` has no default lock.)

## Enabling

`TusServer` already installs `InMemoryLockBackend`; pass `lock_backend` only to tune the timeouts, switch to Redis, or opt out:

```python
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.locks import InMemoryLockBackend

server = TusServer(
    storage=SQLiteStorage(),
    lock_backend=InMemoryLockBackend(),  # the default; shown for the timeout tuning
    lock_ttl_seconds=60.0,  # auto-release if the holder crashes
    lock_wait_seconds=5.0,  # 423 Locked when contention exceeds this
)

# Opt out entirely (no serialization — only safe if writes can never race):
server = TusServer(storage=SQLiteStorage(), lock_backend=None)
```

When a `lock_backend` is set, every PATCH and DELETE acquires a key keyed by `upload_id` before performing the write. Contention beyond `lock_wait_seconds` returns `423 Locked` — the standard "try again later" signal.

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
from resumable_upload.locks.redis_lock import RedisLockBackend

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
        # wait_timeout=0.0 MUST return immediately — see below.
        ...

    def release(self, key: str, token: str) -> None:
        # Idempotent: silently succeed on stale tokens or unknown keys.
        ...
```

The `acquire` token is a capability for `release` — the server passes back exactly the value it received. Implementations must enforce `ttl_seconds` so a crashed holder's lock eventually expires without external intervention.

!!! warning "`wait_timeout=0.0` must not block"

    This is a hard requirement, not a convention. `LockBackend.acquire_async` — which
    the async server path (`handle_request_async`, `TusASGIApp`) uses — bridges your
    sync `acquire` by *polling* it with `wait_timeout=0.0` and awaiting between
    attempts. A single attempt hops to a worker thread; the waiting happens on the
    event loop.

    Note that this is the opposite of some familiar conventions, where a zero or
    absent timeout means "block until acquired" (`threading.Lock.acquire(timeout=-1)`,
    redis-py's `Lock(blocking_timeout=None)`). If your `acquire` blocks on
    `wait_timeout=0.0`, the async path pins an executor worker per waiter for as long
    as it blocks — `lock_wait_seconds` is silently ignored, `423 Locked` is never
    returned, and because the lock *holder*'s storage calls draw from that same
    default executor, enough waiters will starve the very holder that would release
    them.

### Overriding the async path

`acquire_async` / `release_async` are ordinary methods with working defaults, so a
sync-only `acquire` / `release` pair is enough. Override them when your coordinator
has a native async client, or to skip the thread hop entirely:

```python
class MyLockBackend(LockBackend):
    async def acquire_async(
        self, key: str, ttl_seconds: float, wait_timeout: float = 0.0
    ) -> str | None: ...

    async def release_async(self, key: str, token: str) -> None: ...
```

`InMemoryLockBackend` does exactly this — its state is a dict behind a mutex held for
microseconds, so it polls inline and leaves the executor free.
