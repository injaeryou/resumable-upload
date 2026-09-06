"""SQLite-backed storage implementation."""

import contextlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, BinaryIO, Optional

from resumable_upload.storage.base import Storage

logger = logging.getLogger(__name__)

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
            # Migration: add concat_partial_ids (space-separated source partial ids
            # of a final upload, needed to echo Upload-Concat on HEAD)
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE uploads ADD COLUMN concat_partial_ids TEXT")
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
            "concat_partial_ids": (
                row["concat_partial_ids"].split(" ") if row["concat_partial_ids"] else None
            ),
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
        """Mark upload as completed, clean up lock, and report first completion.

        Uses a conditional ``UPDATE ... WHERE completed = 0`` so only the call
        that actually transitions the row returns True (the base contract). If
        two callers assemble the same final (the crash-reclaim clause in
        :meth:`try_assemble_final` can let that happen across processes), only
        the first fires ``on_upload_complete`` — the second gets False.
        """
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            cursor = conn.execute(
                "UPDATE uploads SET completed = 1 WHERE upload_id = ? AND completed = 0",
                (upload_id,),
            )
            conn.commit()
            first_completion = cursor.rowcount == 1
        finally:
            conn.close()
        with self._file_locks_lock:
            self._file_locks.pop(upload_id, None)
        return first_completion

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

    def open_file(self, upload_id: str) -> BinaryIO:
        """Open the uploaded file for streaming; the caller closes it."""
        return open(self.get_file_path(upload_id), "rb")

    def get_file_path(self, upload_id: str) -> str:
        """Get the file path for an upload."""
        return os.path.join(self.upload_dir, upload_id)

    def get_file_info(self, upload_id: str) -> dict[str, Any]:
        """Get file location info for local storage."""
        return {
            "upload_id": upload_id,
            "file_path": self.get_file_path(upload_id),
        }

    # -- Concatenation extension (incl. concatenation-unfinished) -----------

    supports_unfinished_concat = True

    def _create_final_row(
        self,
        final_id: str,
        upload_length: Optional[int],
        metadata: dict[str, str],
        expires_at: Optional[datetime],
        partial_ids: list[str],
    ) -> None:
        """Create a final upload's row atomically, source partials included.

        A single INSERT so a crash can never leave a final row without its
        concat_partial_ids (which HEAD echo and later assembly both need).
        """
        stored_length = self._DEFERRED_LENGTH_SENTINEL if upload_length is None else upload_length
        expires_at_str = expires_at.astimezone(timezone.utc).isoformat() if expires_at else None
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                """
                INSERT INTO uploads (
                    upload_id, upload_length, metadata, expires_at, is_partial,
                    concat_partial_ids
                )
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (
                    final_id,
                    stored_length,
                    json.dumps(metadata),
                    expires_at_str,
                    " ".join(partial_ids),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            with open(self.get_file_path(final_id), "wb"):
                pass
        except OSError:
            self.delete_upload(final_id)
            raise

    def _copy_partials(self, final_id: str, partials: list[dict]) -> int:
        """Stream each partial's file into the final file, in order."""
        final_path = self.get_file_path(final_id)
        buffer_size = 1024 * 1024
        total_length = 0
        with open(final_path, "wb") as dst:
            for p in partials:
                src_path = self.get_file_path(p["upload_id"])
                with open(src_path, "rb") as src:
                    while True:
                        chunk = src.read(buffer_size)
                        if not chunk:
                            break
                        dst.write(chunk)
                        total_length += len(chunk)
        self.update_offset(final_id, total_length)
        self.complete_upload(final_id)
        return total_length

    def concatenate_uploads(
        self,
        final_id: str,
        partial_ids: list[str],
        metadata: dict[str, str],
        *,
        expires_at: Optional[datetime] = None,
        allow_unfinished: bool = False,
    ) -> Optional[int]:
        """Concatenate partial uploads into a single final upload.

        Validates every partial up front (exists, is_partial) before creating
        the final row or writing any bytes, so an error leaves the storage
        unchanged. With ``allow_unfinished=True`` (concatenation-unfinished
        extension), incomplete partials produce a *pending* final instead of
        an error; the return value is then ``None`` and assembly happens later
        via :meth:`try_assemble_final`.
        """
        partials = []
        incomplete = False
        for pid in partial_ids:
            p = self.get_upload(pid)
            if p is None:
                raise ValueError(f"partial upload not found: {pid}")
            if not p.get("is_partial"):
                raise ValueError(f"upload {pid} is not a partial upload")
            if p["offset"] != p["upload_length"]:
                if not allow_unfinished:
                    raise ValueError(f"partial upload {pid} is not complete")
                incomplete = True
            partials.append(p)

        if incomplete:
            # Pending final: length unknown until every partial completes
            # (stored as the deferred sentinel, surfaced as None).
            self._create_final_row(final_id, None, metadata, expires_at, partial_ids)
            return None

        total_length = sum(p["upload_length"] for p in partials)

        # Create the final upload row so get_file_path(final_id) is valid.
        self._create_final_row(final_id, total_length, metadata, expires_at, partial_ids)
        self._copy_partials(final_id, partials)
        return total_length

    def find_pending_finals_for_partial(self, partial_id: str) -> list[str]:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            cursor = conn.execute(
                "SELECT upload_id FROM uploads"
                " WHERE completed = 0 AND concat_partial_ids IS NOT NULL"
                " AND (' ' || concat_partial_ids || ' ') LIKE ?",
                (f"% {partial_id} %",),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [row[0] for row in rows]

    def try_assemble_final(
        self, final_id: str, *, max_total: Optional[int] = None
    ) -> Optional[int]:
        final = self.get_upload(final_id)
        if not final or final["completed"] or not final.get("concat_partial_ids"):
            return None

        partials = []
        for pid in final["concat_partial_ids"]:
            p = self.get_upload(pid)
            if p is None or p["upload_length"] is None or p["offset"] != p["upload_length"]:
                return None  # still pending (or a partial vanished)
            partials.append(p)

        total_length = sum(p["upload_length"] for p in partials)

        if max_total is not None and total_length > max_total:
            # Assembled size would exceed Tus-Max-Size — mirror the sync
            # concat behavior (delete + refuse) since there is no client
            # request left to answer with a 413.
            logger.warning(
                "Pending final %s would be %s bytes (> max %s); deleting",
                final_id,
                total_length,
                max_total,
            )
            self.delete_upload(final_id)
            return None

        # Atomic claim: flip the deferred-length sentinel to the real total.
        # Also reclaimable when upload_length already equals total_length with
        # completed = 0 — a process crash between a previous claim and
        # complete_upload leaves exactly that state, and _copy_partials is
        # idempotent (rewrites the final file from scratch), so retrying is
        # safe. The per-upload lock serializes in-process callers; concurrent
        # PATCHes completing different partials still assemble at most once.
        with self._get_file_lock(final_id):
            conn = sqlite3.connect(self.db_path, timeout=self.timeout)
            try:
                cursor = conn.execute(
                    "UPDATE uploads SET upload_length = ?"
                    " WHERE upload_id = ? AND completed = 0 AND upload_length IN (?, ?)",
                    (total_length, final_id, self._DEFERRED_LENGTH_SENTINEL, total_length),
                )
                conn.commit()
                claimed = cursor.rowcount == 1
            finally:
                conn.close()
            if not claimed:
                return None

            try:
                self._copy_partials(final_id, partials)
            except Exception:
                # Roll the claim back so the final stays *pending* instead of
                # stranded half-assembled; a later HEAD retriggers assembly.
                logger.exception("Assembly of final %s failed; reverting to pending", final_id)
                conn = sqlite3.connect(self.db_path, timeout=self.timeout)
                try:
                    conn.execute(
                        "UPDATE uploads SET upload_length = ?"
                        " WHERE upload_id = ? AND completed = 0",
                        (self._DEFERRED_LENGTH_SENTINEL, final_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return None
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
