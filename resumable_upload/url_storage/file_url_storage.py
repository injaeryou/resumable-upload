"""JSON-file URL storage with fcntl-based cross-process locking."""

import contextlib
import json
import os
import tempfile
import threading
from typing import Optional

from resumable_upload.url_storage.base import URLStorage

try:
    import fcntl as _fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


class FileURLStorage(URLStorage):
    """
    File-based URL storage using JSON.

    Stores upload URLs in a JSON file for persistence across sessions.
    Thread-safe (threading.Lock) and multi-process-safe (fcntl.flock on POSIX).
    """

    def __init__(self, storage_path: str = ".tus_urls.json"):
        """
        Initialize file-based URL storage.

        Args:
            storage_path: Path to JSON file for storing URLs
        """
        self.storage_path = storage_path
        self._lock_file_path = storage_path + ".lock"
        self._lock = threading.Lock()
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Create storage file if it doesn't exist."""
        if not os.path.exists(self.storage_path):
            with open(self.storage_path, "w") as f:
                json.dump({}, f)

    def _load_data(self) -> dict:
        """Load data from storage file."""
        try:
            with open(self.storage_path) as f:
                result: dict = json.load(f)
                return result
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save_data(self, data: dict):
        """Save data to storage file atomically via a temp file + rename."""
        dir_name = os.path.dirname(os.path.abspath(self.storage_path))
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.storage_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    @contextlib.contextmanager
    def _file_lock(self, exclusive: bool = True):
        """Acquire an exclusive or shared fcntl file lock (POSIX only)."""
        if _HAS_FCNTL:
            flag = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
            with open(self._lock_file_path, "w") as lf:
                _fcntl.flock(lf, flag)
                try:
                    yield
                finally:
                    _fcntl.flock(lf, _fcntl.LOCK_UN)
        else:
            yield

    def get_url(self, fingerprint: str) -> Optional[str]:
        """Retrieve upload URL for fingerprint."""
        with self._lock, self._file_lock(exclusive=False):
            data = self._load_data()
        return data.get(fingerprint)

    def set_url(self, fingerprint: str, url: str) -> None:
        """Store upload URL for fingerprint."""
        with self._lock, self._file_lock(exclusive=True):
            data = self._load_data()
            data[fingerprint] = url
            self._save_data(data)

    def remove_url(self, fingerprint: str) -> None:
        """Remove URL for fingerprint."""
        with self._lock, self._file_lock(exclusive=True):
            data = self._load_data()
            if fingerprint in data:
                del data[fingerprint]
                self._save_data(data)
