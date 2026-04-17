"""Distributed lock abstractions for multi-instance TUS deployments.

Single-process ``SQLiteStorage`` already serializes writes via threading locks
and ``fcntl.flock``. Multi-node deployments (Kubernetes replicas, cloud
storage backends with no native locking) need cross-host coordination. The
``LockBackend`` ABC lets ``TusServer`` wrap PATCH / DELETE write paths in a
pluggable lock without caring where the authoritative state lives.
"""

from __future__ import annotations

import secrets
import threading
import time
from abc import ABC, abstractmethod


class LockBackend(ABC):
    """Acquire/release exclusive locks keyed by upload_id.

    Contract:
    - ``acquire`` returns a non-empty token string on success, ``None`` on
      contention or timeout. The token is a capability for subsequent release.
    - ``release`` is idempotent and must silently succeed when called with a
      stale / wrong token or on a key that is not currently held.
    - Implementations must honor ``ttl_seconds`` so a crashed holder's lock
      eventually expires without external intervention.
    """

    @abstractmethod
    def acquire(
        self,
        key: str,
        ttl_seconds: float,
        wait_timeout: float = 0.0,
    ) -> str | None: ...

    @abstractmethod
    def release(self, key: str, token: str) -> None: ...


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
