"""Distributed lock abstraction for multi-instance TUS deployments.

Single-process ``SQLiteStorage`` already serializes writes via threading locks
and ``fcntl.flock``. Multi-node deployments (Kubernetes replicas, cloud
storage backends with no native locking) need cross-host coordination. The
``LockBackend`` ABC lets ``TusServer`` wrap PATCH / DELETE write paths in a
pluggable lock without caring where the authoritative state lives.
"""

from __future__ import annotations

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
