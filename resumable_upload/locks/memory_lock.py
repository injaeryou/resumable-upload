"""In-memory single-process lock backend."""

from __future__ import annotations

import secrets
import threading
import time

from resumable_upload.locks.base import LockBackend


class InMemoryLockBackend(LockBackend):
    """Single-process lock using per-key expiry + one global mutex.

    This is the default backend; it replaces nothing in ``SQLiteStorage``'s
    flock but extends coverage to write paths that don't go through the
    file system (e.g., cloud storage backends).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders: dict[str, tuple[str, float]] = {}

    def acquire(
        self,
        key: str,
        ttl_seconds: float,
        wait_timeout: float = 0.0,
    ) -> str | None:
        deadline = time.monotonic() + wait_timeout
        # Loop until we either grab the lock or give up. Every iteration
        # takes the global mutex, so the TTL check is consistent.
        while True:
            with self._lock:
                entry = self._holders.get(key)
                now = time.monotonic()
                if entry is None or entry[1] <= now:
                    token = secrets.token_hex(16)
                    self._holders[key] = (token, now + ttl_seconds)
                    return token
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def release(self, key: str, token: str) -> None:
        with self._lock:
            entry = self._holders.get(key)
            if entry is not None and entry[0] == token:
                del self._holders[key]
