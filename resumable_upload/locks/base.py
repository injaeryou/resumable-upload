"""Distributed lock abstraction for multi-instance TUS deployments.

Single-process ``SQLiteStorage`` already serializes writes via threading locks
and ``fcntl.flock``. Multi-node deployments (Kubernetes replicas, cloud
storage backends with no native locking) need cross-host coordination. The
``LockBackend`` ABC lets ``TusServer`` wrap PATCH / DELETE write paths in a
pluggable lock without caring where the authoritative state lives.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

#: How long the async waiter sleeps between acquisition attempts.
_POLL_INTERVAL = 0.02


class LockBackend(ABC):
    """Acquire/release exclusive locks keyed by upload_id.

    Contract:
    - ``acquire`` returns a non-empty token string on success, ``None`` on
      contention or timeout. The token is a capability for subsequent release.
    - ``release`` is idempotent and must silently succeed when called with a
      stale / wrong token or on a key that is not currently held.
    - Implementations must honor ``ttl_seconds`` so a crashed holder's lock
      eventually expires without external intervention.
    - ``acquire`` with ``wait_timeout=0`` must not block: it either takes the
      lock immediately or returns ``None``. ``acquire_async`` relies on that
      to poll without pinning a worker thread.
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

    async def acquire_async(
        self,
        key: str,
        ttl_seconds: float,
        wait_timeout: float = 0.0,
    ) -> str | None:
        """Async sibling of :meth:`acquire`.

        Handing the *blocking* ``acquire`` to ``asyncio.to_thread`` would pin
        a worker of the loop's default executor for the whole ``wait_timeout``
        — and the lock *holder*'s storage calls draw from that same pool, so
        enough waiters starve the holder that would release them. Poll the
        non-blocking form instead: each hop occupies a thread only for the
        acquisition attempt itself, and the waiting happens on the event loop.

        Cancellation (an ASGI host dropping the request when the client
        disconnects) can land while an attempt is in flight. A thread cannot be
        interrupted, so that attempt may still take the lock after we are gone —
        the token would be lost and the upload stay locked for the whole TTL.
        Shield the attempt and release whatever it won on the way out.
        """
        deadline = time.monotonic() + wait_timeout
        while True:
            attempt = asyncio.ensure_future(asyncio.to_thread(self.acquire, key, ttl_seconds, 0.0))
            try:
                token = await asyncio.shield(attempt)
            except asyncio.CancelledError:
                attempt.add_done_callback(lambda t: self._discard_orphan(key, t))
                raise
            if token is not None:
                return token
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_POLL_INTERVAL)

    async def release_async(self, key: str, token: str) -> None:
        """Async sibling of :meth:`release`."""
        await asyncio.to_thread(self.release, key, token)

    def _discard_orphan(self, key: str, attempt: asyncio.Future) -> None:
        """Release a lock won by an attempt whose waiter was already cancelled."""
        if attempt.cancelled() or attempt.exception() is not None:
            return
        token = attempt.result()
        if token is not None:
            self.release(key, token)
