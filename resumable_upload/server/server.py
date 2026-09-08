"""Default TUS server class.

``TusServer`` inherits from :class:`TusServerCore` and is the canonical class
to instantiate. It exists as a distinct subclass so future extensions can
land here without forcing every downstream user to update imports.
"""

from typing import Any

from resumable_upload.locks import InMemoryLockBackend
from resumable_upload.server.core import TusServerCore

_UNSET: Any = object()


class TusServer(TusServerCore):
    """TUS server with all standard extensions enabled.

    Differs from :class:`TusServerCore` in one way: it defaults ``lock_backend``
    to an :class:`InMemoryLockBackend` so that a server embedded in a threaded
    or async host (FastAPI, threaded WSGI) serializes concurrent PATCH/DELETE
    on the same upload out of the box — without a lock, two racing PATCHes at
    the same offset can interleave the chunk write and the offset CAS and
    corrupt committed bytes.

    The in-memory lock only covers a single process. **Multi-process or
    multi-node deployments must pass an explicit distributed lock**
    (``RedisLockBackend``); pass ``lock_backend=None`` to opt out entirely.

    The guarantee is bounded by ``lock_ttl_seconds`` (60s by default): a lock
    is reclaimable once its TTL expires, so a PATCH whose chunk write outruns
    the TTL can be joined by a second one and interleave exactly as above. The
    first holder's ``release`` then no-ops on the token mismatch, silently.
    Raise ``lock_ttl_seconds`` past your slowest expected chunk write when
    chunks are large or the storage backend is remote.

    Reserve ``TusServerCore`` for downstream code that wants the minimal,
    lock-free surface.
    """

    def __init__(self, *args: Any, lock_backend: Any = _UNSET, **kwargs: Any) -> None:
        if lock_backend is _UNSET:
            lock_backend = InMemoryLockBackend()
        super().__init__(*args, lock_backend=lock_backend, **kwargs)
