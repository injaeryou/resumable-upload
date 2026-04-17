"""
URL storage interface and implementations for resumable uploads.

Allows storing and retrieving upload URLs based on file fingerprints,
enabling resumable uploads across sessions.
"""

import contextlib
import json
import os
import sqlite3
import tempfile
import threading
from abc import ABC, abstractmethod
from typing import Optional

try:
    import fcntl as _fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


class URLStorage(ABC):
    """Abstract interface for URL storage implementations."""

    @abstractmethod
    def get_url(self, fingerprint: str) -> Optional[str]:
        """
        Retrieve upload URL for a given file fingerprint.

        Args:
            fingerprint: Unique file fingerprint

        Returns:
            Upload URL if found, None otherwise
        """
        pass

    @abstractmethod
    def set_url(self, fingerprint: str, url: str) -> None:
        """
        Store upload URL for a given file fingerprint.

        Args:
            fingerprint: Unique file fingerprint
            url: Upload URL to store
        """
        pass

    @abstractmethod
    def remove_url(self, fingerprint: str) -> None:
        """
        Remove stored URL for a given file fingerprint.

        Args:
            fingerprint: Unique file fingerprint
        """
        pass


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


class SQLiteURLStorage(URLStorage):
    """SQLite-backed URL storage.

    Durable, concurrent-safe without application-level locking (SQLite's
    own locks serialize writes). Preferred over FileURLStorage for
    multi-process clients on the same host.
    """

    def __init__(self, db_path: str = "tus_urls.db", timeout: float = 5.0) -> None:
        self.db_path = db_path
        self.timeout = timeout
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS url_map (
                    fingerprint TEXT PRIMARY KEY,
                    url TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def get_url(self, fingerprint: str) -> Optional[str]:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            cur = conn.execute("SELECT url FROM url_map WHERE fingerprint = ?", (fingerprint,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def set_url(self, fingerprint: str, url: str) -> None:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                """
                INSERT INTO url_map (fingerprint, url) VALUES (?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET url = excluded.url
                """,
                (fingerprint, url),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_url(self, fingerprint: str) -> None:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute("DELETE FROM url_map WHERE fingerprint = ?", (fingerprint,))
            conn.commit()
        finally:
            conn.close()
