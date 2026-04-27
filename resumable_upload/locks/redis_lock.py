"""Redis-backed LockBackend using ``SET NX PX`` + Lua release.

The classic pattern: ``SET key token NX PX <ttl>`` acquires the lock
atomically if absent, with a TTL that releases the lock if the holder
crashes. Release uses a Lua script so the "check token, then delete"
sequence runs atomically on the server.

Requires the optional ``[redis]`` extra (`pip install resumable-upload[redis]`).
"""

from __future__ import annotations

import contextlib
import secrets
import time

from resumable_upload.locks.base import LockBackend

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class RedisLockBackend(LockBackend):
    """Distributed lock backed by Redis.

    Bring your own ``redis.Redis`` (or compatible) client — the backend does
    not configure connection pooling or retry policy. Works with any Redis
    deployment, including cluster (the Lua release script operates on a
    single key).
    """

    def __init__(self, client, key_prefix: str = "tus:lock:") -> None:
        self._client = client
        self._prefix = key_prefix
        # register_script handles SHA caching + EVAL fallback transparently,
        # and works uniformly across redis-py versions and fakeredis.
        self._release_script = self._client.register_script(_RELEASE_LUA)

    def _k(self, key: str) -> str:
        return self._prefix + key

    def acquire(
        self,
        key: str,
        ttl_seconds: float,
        wait_timeout: float = 0.0,
    ) -> str | None:
        deadline = time.monotonic() + wait_timeout
        while True:
            token = secrets.token_hex(16)
            got = self._client.set(self._k(key), token, nx=True, px=int(ttl_seconds * 1000))
            if got:
                return token
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def release(self, key: str, token: str) -> None:
        # Token mismatch or transient network error: release is idempotent by
        # contract, so swallow everything. The Lua script already enforces
        # the "only delete if I'm the holder" invariant atomically.
        with contextlib.suppress(Exception):
            self._release_script(keys=[self._k(key)], args=[token])
