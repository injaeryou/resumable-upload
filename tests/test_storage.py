"""Test suite for storage module."""

import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from resumable_upload.storage import SQLiteStorage


class TestSQLiteStorage:
    """Tests for SQLiteStorage."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def storage(self, temp_dir):
        """Create a storage instance for tests."""
        db_path = os.path.join(temp_dir, "test.db")
        upload_dir = os.path.join(temp_dir, "uploads")
        return SQLiteStorage(db_path=db_path, upload_dir=upload_dir)

    def test_create_upload(self, storage):
        """Test creating an upload."""
        upload_id = "test-upload-1"
        upload_length = 1024
        metadata = {"filename": "test.txt"}

        storage.create_upload(upload_id, upload_length, metadata)

        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["upload_id"] == upload_id
        assert upload["upload_length"] == upload_length
        assert upload["offset"] == 0
        assert upload["metadata"] == metadata
        assert upload["completed"] is False

    def test_get_nonexistent_upload(self, storage):
        """Test getting a non-existent upload."""
        upload = storage.get_upload("nonexistent")
        assert upload is None

    def test_update_offset(self, storage):
        """Test updating upload offset."""
        upload_id = "test-upload-2"
        storage.create_upload(upload_id, 1024, {})

        storage.update_offset(upload_id, 512)
        upload = storage.get_upload(upload_id)
        assert upload["offset"] == 512
        assert upload["completed"] is False

        storage.update_offset(upload_id, 1024)
        upload = storage.get_upload(upload_id)
        assert upload["offset"] == 1024
        assert upload["completed"] is False

        storage.complete_upload(upload_id)
        upload = storage.get_upload(upload_id)
        assert upload["completed"] is True

    def test_update_offset_atomic_success(self, storage):
        """CAS advances the offset and reports success when expected matches."""
        storage.create_upload("cas-ok", 1024, {})
        assert storage.update_offset_atomic("cas-ok", 0, 512) is True
        assert storage.get_upload("cas-ok")["offset"] == 512

    def test_update_offset_atomic_conflict(self, storage):
        """CAS is a no-op returning False when the expected offset is stale."""
        storage.create_upload("cas-stale", 1024, {})
        storage.update_offset("cas-stale", 256)
        # Client still thinks offset is 0 (stale) -> reject, don't clobber.
        assert storage.update_offset_atomic("cas-stale", 0, 512) is False
        assert storage.get_upload("cas-stale")["offset"] == 256

    def test_update_offset_atomic_race_exactly_one_winner(self, storage):
        """Two threads racing the same expected offset: exactly one CAS wins.

        This is the whole reason update_offset_atomic exists (TUS invariant:
        concurrent PATCH with a stale offset must be rejected). A non-atomic
        read-then-write would let both win and double-advance the offset.
        """
        storage.create_upload("cas-race", 4096, {})
        barrier = threading.Barrier(2)
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()  # release both threads simultaneously
            won = storage.update_offset_atomic("cas-race", 0, 256)
            with lock:
                results.append(won)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1, f"expected exactly one winner, got {results}"
        assert storage.get_upload("cas-race")["offset"] == 256

    def test_complete_upload_returns_true_only_on_first_completion(self, storage):
        # C3: the crash-reclaim clause in try_assemble_final can let two callers
        # assemble the same final; complete_upload must transition once so the
        # on_upload_complete hook (gated on this return) fires exactly once.
        storage.create_upload("done", 5, {})
        assert storage.complete_upload("done") is True  # 0 -> 1
        assert storage.complete_upload("done") is False  # already completed
        assert storage.get_upload("done")["completed"] is True

    def test_write_and_read_chunk(self, storage):
        """Test writing and reading chunks."""
        upload_id = "test-upload-3"
        storage.create_upload(upload_id, 100, {})

        # Write chunks
        storage.write_chunk(upload_id, 0, b"Hello ")
        storage.write_chunk(upload_id, 6, b"World!")

        # Read file
        data = storage.read_file(upload_id)
        assert data == b"Hello World!"

    def test_delete_upload(self, storage, temp_dir):
        """Test deleting an upload."""
        upload_id = "test-upload-4"
        storage.create_upload(upload_id, 1024, {})

        file_path = storage.get_file_path(upload_id)
        assert os.path.exists(file_path)

        storage.delete_upload(upload_id)
        assert storage.get_upload(upload_id) is None
        assert not os.path.exists(file_path)

    def test_get_file_path(self, storage, temp_dir):
        """Test getting file path."""
        upload_id = "test-upload-5"
        expected_path = os.path.join(temp_dir, "uploads", upload_id)
        assert storage.get_file_path(upload_id) == expected_path

    # --- Phase 2.1: Expiration tests ---

    def test_expires_at_stored_in_db(self, storage):
        """expires_at is stored and returned by get_upload."""
        upload_id = str(uuid.uuid4())
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        storage.create_upload(upload_id, 100, {}, expires_at=expires)

        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["expires_at"] is not None
        # Allow 2-second tolerance for processing time
        assert abs((upload["expires_at"] - expires).total_seconds()) < 2

    def test_get_expired_uploads_returns_ids(self, storage):
        """get_expired_uploads returns IDs of expired uploads only."""
        future_id = str(uuid.uuid4())
        past_id = str(uuid.uuid4())
        no_expiry_id = str(uuid.uuid4())

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        past = datetime.now(timezone.utc) - timedelta(seconds=1)

        storage.create_upload(future_id, 100, {}, expires_at=future)
        storage.create_upload(past_id, 100, {}, expires_at=past)
        storage.create_upload(no_expiry_id, 100, {})

        expired = storage.get_expired_uploads()
        assert past_id in expired
        assert future_id not in expired
        assert no_expiry_id not in expired

    def test_cleanup_expired_uploads_removes_files(self, storage, temp_dir):
        """cleanup_expired_uploads deletes expired uploads and their files."""
        past_id = str(uuid.uuid4())
        future_id = str(uuid.uuid4())
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)

        storage.create_upload(past_id, 100, {}, expires_at=past)
        storage.create_upload(future_id, 100, {}, expires_at=future)

        past_file = storage.get_file_path(past_id)
        assert os.path.exists(past_file)

        count = storage.cleanup_expired_uploads()
        assert count == 1
        assert storage.get_upload(past_id) is None
        assert not os.path.exists(past_file)
        # Non-expired upload should remain
        assert storage.get_upload(future_id) is not None

    def test_metadata_special_characters_roundtrip(self, storage):
        """Metadata with unicode, quotes, and backslashes round-trips correctly."""
        upload_id = str(uuid.uuid4())
        metadata = {
            "filename": "résumé café.txt",
            "path": "C:\\Users\\test\\file.bin",
            "note": 'has "quotes" and\nnewlines',
        }
        storage.create_upload(upload_id, 100, metadata)
        retrieved = storage.get_upload(upload_id)
        assert retrieved["metadata"] == metadata

    def test_create_upload_rolls_back_db_if_file_creation_fails(self, storage, temp_dir):
        """If upload file cannot be created, the DB record is also rolled back."""
        upload_id = str(uuid.uuid4())
        upload_dir = os.path.join(temp_dir, "uploads")

        # Make the upload directory read-only so file creation fails
        os.chmod(upload_dir, 0o444)
        try:
            with pytest.raises(OSError):
                storage.create_upload(upload_id, 100, {})
            # DB record must not exist
            assert storage.get_upload(upload_id) is None
        finally:
            os.chmod(upload_dir, 0o755)

    # --- is_partial flag (Phase A Task 1) ---

    def test_create_upload_with_is_partial_flag(self, storage):
        """is_partial=True is persisted and surfaced by get_upload."""
        upload_id = "11111111-1111-1111-1111-111111111111"
        storage.create_upload(upload_id, 10, {}, is_partial=True)
        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["is_partial"] is True

    def test_create_upload_defaults_is_partial_false(self, storage):
        """is_partial defaults to False when the kwarg is omitted."""
        upload_id = "22222222-2222-2222-2222-222222222222"
        storage.create_upload(upload_id, 10, {})
        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["is_partial"] is False

    def test_legacy_db_migration_adds_is_partial(self, temp_dir):
        """Initializing over a DB without is_partial adds the column via migration."""
        db_path = os.path.join(temp_dir, "legacy.db")
        upload_dir = os.path.join(temp_dir, "uploads_legacy")
        os.makedirs(upload_dir, exist_ok=True)

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE uploads (
                upload_id TEXT PRIMARY KEY,
                upload_length INTEGER NOT NULL,
                offset INTEGER DEFAULT 0,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed BOOLEAN DEFAULT 0
            )
            """
        )
        conn.commit()
        conn.close()

        storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
        upload_id = str(uuid.uuid4())
        storage.create_upload(upload_id, 100, {}, is_partial=True)
        upload = storage.get_upload(upload_id)
        assert upload["is_partial"] is True

    def test_existing_db_migration_adds_expires_at(self, temp_dir):
        """Re-initializing an old DB (without expires_at) adds the column."""
        db_path = os.path.join(temp_dir, "old.db")
        upload_dir = os.path.join(temp_dir, "uploads_old")
        os.makedirs(upload_dir, exist_ok=True)

        # Create a DB without expires_at column (simulating old schema)
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE uploads (
                upload_id TEXT PRIMARY KEY,
                upload_length INTEGER NOT NULL,
                offset INTEGER DEFAULT 0,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed BOOLEAN DEFAULT 0
            )
            """
        )
        conn.commit()
        conn.close()

        # Initializing SQLiteStorage should add expires_at via migration
        storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)

        # Verify column exists by using it
        upload_id = str(uuid.uuid4())
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        storage.create_upload(upload_id, 100, {}, expires_at=future)

        upload = storage.get_upload(upload_id)
        assert upload["expires_at"] is not None
