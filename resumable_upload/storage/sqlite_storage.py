"""SQLite-backed storage implementation."""

import contextlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from resumable_upload.storage.base import Storage

try:
    import fcntl as _fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


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
                    completed BOOLEAN DEFAULT 0,
                    is_partial BOOLEAN DEFAULT 0
                )
                """
            )
            # Migration: add expires_at column for existing databases
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE uploads ADD COLUMN expires_at TIMESTAMP")
            # Migration: add is_partial column (TUS concatenation extension)
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE uploads ADD COLUMN is_partial BOOLEAN DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_uploads_expires_at"
                " ON uploads (expires_at) WHERE expires_at IS NOT NULL"
            )
            conn.commit()
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()

    # Sentinel stored in the NOT NULL upload_length column when a caller passes
    # None (Upload-Defer-Length). Surfaces as None on read; callers should not
    # see the sentinel.
    _DEFERRED_LENGTH_SENTINEL = -1

    def create_upload(
        self,
        upload_id: str,
        upload_length: Optional[int],
        metadata: dict[str, str],
        expires_at: Optional[datetime] = None,
        is_partial: bool = False,
    ) -> None:
        """Create a new upload entry. ``upload_length=None`` enables defer-length."""
        stored_length = self._DEFERRED_LENGTH_SENTINEL if upload_length is None else upload_length
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            expires_at_str = expires_at.astimezone(timezone.utc).isoformat() if expires_at else None
            conn.execute(
                """
                INSERT INTO uploads (
                    upload_id, upload_length, metadata, expires_at, is_partial
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    stored_length,
                    json.dumps(metadata),
                    expires_at_str,
                    int(is_partial),
                ),
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

        raw_length = row["upload_length"]
        upload_length = None if raw_length == self._DEFERRED_LENGTH_SENTINEL else raw_length
        return {
            "upload_id": row["upload_id"],
            "upload_length": upload_length,
            "offset": row["offset"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            "completed": bool(row["completed"]),
            "expires_at": expires_at,
            "is_partial": bool(row["is_partial"]),
        }

    def update_offset(self, upload_id: str, offset: int) -> None:
        """Update the current offset of an upload."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                "UPDATE uploads SET offset = ? WHERE upload_id = ?",
                (offset, upload_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_upload_length(self, upload_id: str, upload_length: int) -> None:
        """Commit the final length of a deferred-length upload."""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                "UPDATE uploads SET upload_length = ? WHERE upload_id = ?",
                (upload_length, upload_id),
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

    def concatenate_uploads(
        self,
        final_id: str,
        partial_ids: list[str],
        metadata: dict[str, str],
        *,
        expires_at: Optional[datetime] = None,
    ) -> int:
        """Concatenate partial uploads into a single final upload.

        Validates every partial up front (exists, is_partial, fully received)
        before creating the final row or writing any bytes, so an error leaves
        the storage unchanged.
        """
        partials = []
        for pid in partial_ids:
            p = self.get_upload(pid)
            if p is None:
                raise ValueError(f"partial upload not found: {pid}")
            if not p.get("is_partial"):
                raise ValueError(f"upload {pid} is not a partial upload")
            if p["offset"] != p["upload_length"]:
                raise ValueError(f"partial upload {pid} is not complete")
            partials.append(p)

        total_length = sum(p["upload_length"] for p in partials)

        # Create the final upload row so get_file_path(final_id) is valid.
        self.create_upload(final_id, total_length, metadata, expires_at, is_partial=False)

        # Stream each partial's file into the final file, in order.
        final_path = self.get_file_path(final_id)
        buffer_size = 1024 * 1024
        with open(final_path, "wb") as dst:
            for p in partials:
                src_path = self.get_file_path(p["upload_id"])
                with open(src_path, "rb") as src:
                    while True:
                        chunk = src.read(buffer_size)
                        if not chunk:
                            break
                        dst.write(chunk)

        self.update_offset(final_id, total_length)
        self.complete_upload(final_id)
        return total_length

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
