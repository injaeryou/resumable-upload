"""Hash algorithm registry for the TUS Upload-Checksum extension.

Keeps the supported-algorithm table out of the ``TusServer`` wire-handling
code so backends and clients can share the validation logic. Zero runtime
deps: only ``hashlib`` from the standard library.
"""

from __future__ import annotations

import hashlib
from typing import Callable

_DEFAULT_ALGORITHMS: dict[str, Callable[[], hashlib._Hash]] = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
    "md5": hashlib.md5,
}


class ChecksumAlgorithms:
    """Thread-safe algorithm registry with TUS-compliant verification."""

    def __init__(self, enabled: tuple[str, ...] = ("sha1",)) -> None:
        self._enabled = tuple(a.lower() for a in enabled)
        self._factories = dict(_DEFAULT_ALGORITHMS)
        unknown = set(self._enabled) - set(self._factories)
        if unknown:
            raise ValueError(f"Unknown checksum algorithms: {sorted(unknown)}")

    @property
    def enabled(self) -> tuple[str, ...]:
        return self._enabled

    def is_supported(self, name: str) -> bool:
        return name.lower() in self._enabled

    def compute(self, name: str, data: bytes) -> str:
        """Compute the hex digest of ``data`` with ``name``."""
        hasher = self._factories[name.lower()]()
        hasher.update(data)
        return hasher.hexdigest()
