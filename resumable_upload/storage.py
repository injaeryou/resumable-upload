"""Storage backend for managing upload state."""

import contextlib
import json
import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import fcntl as _fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


class Storage(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def create_upload(
        self,
        upload_id: str,
        upload_length: int,
        metadata: dict[str, str],
        expires_at: Optional[datetime] = None,
    ) -> None:
        """Create a new upload entry."""
        pass

    @abstractmethod
    def get_upload(self, upload_id: str) -> Optional[dict[str, Any]]:
        """Get upload information."""
        pass

    @abstractmethod
    def update_offset(self, upload_id: str, offset: int) -> None:
        """Update the current offset of an upload."""
        pass

    def update_offset_atomic(self, upload_id: str, expected_offset: int, new_offset: int) -> bool:
        """Update offset only if current value equals expected_offset.

        Returns True on success, False if offset already changed (concurrent conflict).
        Default implementation is non-atomic; SQLiteStorage overrides with a
        single conditional UPDATE for true atomicity.
        """
        upload = self.get_upload(upload_id)
        if upload is None or upload["offset"] != expected_offset:
            return False
        self.update_offset(upload_id, new_offset)
        return True

    def complete_upload(self, upload_id: str) -> bool:  # noqa: B027
        """Finalize the upload after all data has been received.

        Returns True if this call transitioned the upload to completed state,
        False if it was already completed (idempotent guard against double-completion).
        Cloud storage backends override this to complete multipart uploads,
        compose objects, or commit block lists.
        """
        return True

    @abstractmethod
    def delete_upload(self, upload_id: str) -> None:
        """Delete an upload entry."""
        pass

    @abstractmethod
    def write_chunk(self, upload_id: str, offset: int, data: bytes) -> None:
        """Write a chunk of data to the upload file."""
        pass

    @abstractmethod
    def read_file(self, upload_id: str) -> bytes:
        """Read the complete uploaded file."""
        pass

    def get_file_path(self, upload_id: str) -> str:
        """Get the file path for an upload.

        Only meaningful for local storage backends. Cloud backends
        should raise NotImplementedError.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support local file paths")

    def get_file_info(self, upload_id: str) -> dict[str, Any]:
        """Get backend-specific file location info.

        Returns a dict with at least 'upload_id'. Backends add their own
        keys (e.g. 'file_path' for local, 'bucket'/'key' for S3).
        """
        return {"upload_id": upload_id}

    @abstractmethod
    def get_expired_uploads(self) -> list[str]:
        """Get list of expired upload IDs."""
        pass

    @abstractmethod
    def cleanup_expired_uploads(self) -> int:
        """Delete expired uploads and return count deleted."""
        pass


class SQLiteStorage(Storage):
    """SQLite-based storage backend."""

    def __init__(
        self, db_path: str = "uploads.db", upload_dir: str = "uploads", timeout: float = 10.0
    ):
        """Initialize SQLite storage.

        Args:
            db_path: Path to SQLite database file
            upload_dir: Directory to store uploaded files
            timeout: SQLite connection timeout in seconds
        """
        self.db_path = db_path
        self.upload_dir = upload_dir
        self.timeout = timeout
        os.makedirs(upload_dir, exist_ok=True)
        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_lock = threading.Lock()
        self._init_db()

    def _get_file_lock(self, upload_id: str) -> threading.Lock:
        """Get or create a per-upload threading lock."""
        with self._file_locks_lock:
            if upload_id not in self._file_locks:
                self._file_locks[upload_id] = threading.Lock()
            return self._file_locks[upload_id]

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY,
                    upload_length INTEGER NOT NULL,
                    offset INTEGER DEFAULT 0,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed BOOLEAN DEFAULT 0
                )
                """
            )
            # Migration: add expires_at column for existing databases
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE uploads ADD COLUMN expires_at TIMESTAMP")
            conn.commit()
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()

    def create_upload(
        self,
        upload_id: str,
        upload_length: int,
        metadata: dict[str, str],
        expires_at: Optional[datetime] = None,
    ) -> None:
        """Create a new upload entry."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            expires_at_str = expires_at.astimezone(timezone.utc).isoformat() if expires_at else None
            conn.execute(
                """
                INSERT INTO uploads (upload_id, upload_length, metadata, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (upload_id, upload_length, json.dumps(metadata), expires_at_str),
            )
            conn.commit()
        finally:
            conn.close()

        # Create empty file; roll back DB record if file creation fails
        file_path = self.get_file_path(upload_id)
        try:
            with open(file_path, "wb"):
                pass
        except OSError:
            self.delete_upload(upload_id)
            raise

    def get_upload(self, upload_id: str) -> Optional[dict[str, Any]]:
        """Get upload information."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM uploads WHERE upload_id = ?", (upload_id,))
            row = cursor.fetchone()
        finally:
            conn.close()

        if row is None:
            return None

        expires_at = None
        raw_expires = row["expires_at"]
        if raw_expires:
            try:
                dt = datetime.fromisoformat(raw_expires)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                expires_at = dt
            except (ValueError, AttributeError):
                pass

        return {
            "upload_id": row["upload_id"],
            "upload_length": row["upload_length"],
            "offset": row["offset"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            "completed": bool(row["completed"]),
            "expires_at": expires_at,
        }

    def update_offset(self, upload_id: str, offset: int) -> None:
        """Update the current offset of an upload."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                "UPDATE uploads SET offset = ?, completed = (? >= upload_length)"
                " WHERE upload_id = ?",
                (offset, offset, upload_id),
            )
            conn.commit()
        finally:
            conn.close()

    def update_offset_atomic(self, upload_id: str, expected_offset: int, new_offset: int) -> bool:
        """Atomically update offset; returns False on concurrent conflict."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            cursor = conn.execute(
                "UPDATE uploads SET offset = ? WHERE upload_id = ? AND offset = ?",
                (new_offset, upload_id, expected_offset),
            )
            conn.commit()
            return cursor.rowcount != 0
        finally:
            conn.close()

    def complete_upload(self, upload_id: str) -> bool:
        """Mark upload as completed, clean up lock, and return True.

        Sets completed=1 in the DB (idempotent) and removes the per-upload
        lock entry. Always returns True for local storage.
        """
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                "UPDATE uploads SET completed = 1 WHERE upload_id = ?",
                (upload_id,),
            )
            conn.commit()
        finally:
            conn.close()
        with self._file_locks_lock:
            self._file_locks.pop(upload_id, None)
        return True

    def delete_upload(self, upload_id: str) -> None:
        """Delete an upload entry."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute("DELETE FROM uploads WHERE upload_id = ?", (upload_id,))
            conn.commit()
        finally:
            conn.close()

        # Delete file if exists
        file_path = self.get_file_path(upload_id)
        if os.path.exists(file_path):
            os.remove(file_path)

        # Remove per-upload lock entry
        with self._file_locks_lock:
            self._file_locks.pop(upload_id, None)

    def write_chunk(self, upload_id: str, offset: int, data: bytes) -> None:
        """Write a chunk of data to the upload file.

        Thread-safe and multi-process-safe: uses a per-upload threading.Lock
        (in-process) combined with fcntl.flock (cross-process, POSIX only).
        """
        file_path = self.get_file_path(upload_id)
        if not os.path.exists(file_path):
            with open(file_path, "wb"):
                pass
        lock = self._get_file_lock(upload_id)
        with lock, open(file_path, "r+b") as f:
            if _HAS_FCNTL:
                _fcntl.flock(f, _fcntl.LOCK_EX)
            f.seek(offset)
            f.write(data)

    def read_file(self, upload_id: str) -> bytes:
        """Read the complete uploaded file."""
        file_path = self.get_file_path(upload_id)
        with open(file_path, "rb") as f:
            return f.read()

    def get_file_path(self, upload_id: str) -> str:
        """Get the file path for an upload."""
        return os.path.join(self.upload_dir, upload_id)

    def get_file_info(self, upload_id: str) -> dict[str, Any]:
        """Get file location info for local storage."""
        return {
            "upload_id": upload_id,
            "file_path": self.get_file_path(upload_id),
        }

    def get_expired_uploads(self) -> list[str]:
        """Get list of expired upload IDs."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "SELECT upload_id FROM uploads WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [row[0] for row in rows]

    def cleanup_expired_uploads(self) -> int:
        """Delete expired uploads and return count deleted."""
        expired_ids = self.get_expired_uploads()
        for upload_id in expired_ids:
            self.delete_upload(upload_id)
        return len(expired_ids)
