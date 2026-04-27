"""Process-local in-memory URL storage."""

import threading
from typing import Optional

from resumable_upload.url_storage.base import URLStorage


class InMemoryURLStorage(URLStorage):
    """Thread-safe in-memory URL storage.

    Fast and process-local. Everything is forgotten when the process exits
    — use for tests or short-lived upload sessions where cross-session
    resume isn't needed.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get_url(self, fingerprint: str) -> Optional[str]:
        with self._lock:
            return self._data.get(fingerprint)

    def set_url(self, fingerprint: str, url: str) -> None:
        with self._lock:
            self._data[fingerprint] = url

    def remove_url(self, fingerprint: str) -> None:
        with self._lock:
            self._data.pop(fingerprint, None)
