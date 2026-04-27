"""Tests for GCSStorage backend using mock GCS client."""

from datetime import datetime, timedelta, timezone

import pytest


class FakeBlob:
    """In-memory blob that mimics google.cloud.storage.Blob."""

    def __init__(self, name: str, bucket: "FakeBucket"):
        self.name = name
        self._bucket = bucket

    def upload_from_string(self, data, content_type=None):
        if isinstance(data, str):
            data = data.encode()
        self._bucket._blobs[self.name] = data

    def download_as_bytes(self):
        from google.cloud.exceptions import NotFound

        if self.name not in self._bucket._blobs:
            raise NotFound(f"Blob {self.name} not found")
        return self._bucket._blobs[self.name]

    def delete(self):
        from google.cloud.exceptions import NotFound

        if self.name not in self._bucket._blobs:
            raise NotFound(f"Blob {self.name} not found")
        del self._bucket._blobs[self.name]

    def compose(self, sources):
        combined = b""
        for src in sources:
            combined += self._bucket._blobs[src.name]
        self._bucket._blobs[self.name] = combined


class FakeBucket:
    """In-memory bucket that mimics google.cloud.storage.Bucket."""

    def __init__(self, name: str):
        self.name = name
        self._blobs: dict[str, bytes] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(name, self)

    def copy_blob(self, source_blob, destination_bucket, destination_key):
        destination_bucket._blobs[destination_key] = self._blobs[source_blob.name]


class FakeGCSClient:
    """In-memory GCS client for testing."""

    def __init__(self):
        self._buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        if name not in self._buckets:
            self._buckets[name] = FakeBucket(name)
        return self._buckets[name]

    def list_blobs(self, bucket, prefix=""):
        blob_names = sorted(bucket._blobs.keys())
        result = []
        for name in blob_names:
            if name.startswith(prefix):
                blob = FakeBlob(name, bucket)
                result.append(blob)
        return result


# We need to mock the google.cloud imports before importing GCSStorage
@pytest.fixture(autouse=True)
def mock_gcs_imports():
    """Mock google.cloud.storage and google.cloud.exceptions modules."""
    import types

    # Create mock google.cloud.exceptions module with real NotFound
    class NotFound(Exception):
        pass

    exceptions_mod = types.ModuleType("google.cloud.exceptions")
    exceptions_mod.NotFound = NotFound

    # Create mock google.cloud.storage module
    storage_mod = types.ModuleType("google.cloud.storage")
    storage_mod.Client = FakeGCSClient

    google_mod = types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    google_mod.cloud = cloud_mod
    cloud_mod.storage = storage_mod
    cloud_mod.exceptions = exceptions_mod

    import sys

    saved = {}
    for mod_name in ["google", "google.cloud", "google.cloud.storage", "google.cloud.exceptions"]:
        saved[mod_name] = sys.modules.get(mod_name)
        sys.modules[mod_name] = {
            "google": google_mod,
            "google.cloud": cloud_mod,
            "google.cloud.storage": storage_mod,
            "google.cloud.exceptions": exceptions_mod,
        }[mod_name]

    yield NotFound

    # Restore
    for mod_name, original in saved.items():
        if original is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = original


TEST_BUCKET = "test-tus-uploads"


@pytest.fixture
def gcs_client():
    return FakeGCSClient()


@pytest.fixture
def storage(gcs_client):
    # Force reimport to pick up mocked modules
    import importlib

    import resumable_upload.storage.gcs_storage as mod

    importlib.reload(mod)
    s = mod.GCSStorage(
        bucket=TEST_BUCKET,
        gcs_client=gcs_client,
        prefix="uploads",
        part_size=10,  # Small for testing
    )
    s._flush_size = 10  # Override for testing (real GCS enforces 5MB minimum)
    return s


# -- Basic CRUD operations --------------------------------------------------


class TestGCSStorageCreate:
    def test_create_upload(self, storage):
        storage.create_upload("test-id", 1024, {"filename": "test.bin"})
        upload = storage.get_upload("test-id")
        assert upload is not None
        assert upload["upload_id"] == "test-id"
        assert upload["upload_length"] == 1024
        assert upload["offset"] == 0
        assert upload["metadata"]["filename"] == "test.bin"
        assert upload["completed"] is False

    def test_create_upload_with_expiry(self, storage):
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        storage.create_upload("exp-id", 100, {}, expires_at=expires)
        upload = storage.get_upload("exp-id")
        assert upload["expires_at"] is not None
        assert upload["expires_at"] > datetime.now(timezone.utc)

    def test_get_nonexistent_upload(self, storage):
        assert storage.get_upload("nonexistent") is None


class TestGCSStorageWrite:
    def test_write_chunk_buffering(self, storage):
        """Chunks smaller than part_size are buffered."""
        storage.create_upload("u2", 20, {})
        storage.write_chunk("u2", 0, b"ABCDE")  # 5 bytes < 10 part_size
        storage.update_offset("u2", 5)

        upload = storage.get_upload("u2")
        assert upload["offset"] == 5
        assert not upload["completed"]

    def test_write_single_chunk(self, storage):
        """Write data >= part_size flushes as a part."""
        data = b"A" * 20
        storage.create_upload("u1", len(data), {})
        storage.write_chunk("u1", 0, data[:10])
        storage.update_offset("u1", 10)

        upload = storage.get_upload("u1")
        assert upload["offset"] == 10

    def test_full_upload_flow(self, storage):
        """Complete upload with multiple chunks then read back data."""
        data = b"Hello, GCS Storage!"
        storage.create_upload("full", len(data), {"filename": "test.txt"})

        offset = 0
        chunk_size = 10
        while offset < len(data):
            chunk = data[offset : offset + chunk_size]
            storage.write_chunk("full", offset, chunk)
            offset += len(chunk)
            storage.update_offset("full", offset)

        storage.complete_upload("full")

        upload = storage.get_upload("full")
        assert upload["completed"] is True

        result = storage.read_file("full")
        assert result == data

    def test_single_shot_upload(self, storage):
        """Upload where all data comes in one chunk smaller than part_size."""
        data = b"one shot"
        storage.create_upload("oneshot", len(data), {})
        storage.write_chunk("oneshot", 0, data)
        storage.update_offset("oneshot", len(data))
        storage.complete_upload("oneshot")

        assert storage.read_file("oneshot") == data


class TestGCSStorageDelete:
    def test_delete_upload(self, storage):
        storage.create_upload("del-id", 100, {})
        assert storage.get_upload("del-id") is not None
        storage.delete_upload("del-id")
        assert storage.get_upload("del-id") is None

    def test_delete_nonexistent_upload(self, storage):
        """Deleting non-existent upload should not raise."""
        storage.delete_upload("nonexistent")


# -- Offset operations -------------------------------------------------------


class TestGCSStorageOffset:
    def test_update_offset(self, storage):
        storage.create_upload("off-id", 100, {})
        storage.update_offset("off-id", 50)
        upload = storage.get_upload("off-id")
        assert upload["offset"] == 50
        assert not upload["completed"]

    def test_update_offset_does_not_mark_completed(self, storage):
        storage.create_upload("comp-id", 100, {})
        storage.update_offset("comp-id", 100)
        upload = storage.get_upload("comp-id")
        assert upload["completed"] is False

    def test_complete_upload_marks_completed(self, storage):
        storage.create_upload("comp-id2", 100, {})
        storage.update_offset("comp-id2", 100)
        assert storage.complete_upload("comp-id2") is True
        upload = storage.get_upload("comp-id2")
        assert upload["completed"] is True

    def test_update_offset_atomic_success(self, storage):
        storage.create_upload("atom-id", 100, {})
        assert storage.update_offset_atomic("atom-id", 0, 50) is True
        upload = storage.get_upload("atom-id")
        assert upload["offset"] == 50

    def test_update_offset_atomic_conflict(self, storage):
        storage.create_upload("conflict-id", 100, {})
        storage.update_offset("conflict-id", 30)
        assert storage.update_offset_atomic("conflict-id", 0, 50) is False
        upload = storage.get_upload("conflict-id")
        assert upload["offset"] == 30


# -- File info ---------------------------------------------------------------


class TestGCSStorageFileInfo:
    def test_get_file_info(self, storage):
        storage.create_upload("info-id", 100, {})
        info = storage.get_file_info("info-id")
        assert info["upload_id"] == "info-id"
        assert info["bucket"] == TEST_BUCKET
        assert "key" in info

    def test_get_file_path_raises(self, storage):
        with pytest.raises(NotImplementedError):
            storage.get_file_path("any-id")


# -- Expiration --------------------------------------------------------------


class TestGCSStorageExpiration:
    def test_get_expired_uploads(self, storage):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)

        storage.create_upload("expired", 100, {}, expires_at=past)
        storage.create_upload("valid", 100, {}, expires_at=future)
        storage.create_upload("no-expiry", 100, {})

        expired = storage.get_expired_uploads()
        assert "expired" in expired
        assert "valid" not in expired
        assert "no-expiry" not in expired

    def test_cleanup_expired_uploads(self, storage):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        storage.create_upload("exp1", 100, {}, expires_at=past)
        storage.create_upload("exp2", 100, {}, expires_at=past)
        storage.create_upload("keep", 100, {})

        count = storage.cleanup_expired_uploads()
        assert count == 2
        assert storage.get_upload("exp1") is None
        assert storage.get_upload("exp2") is None
        assert storage.get_upload("keep") is not None


# -- Integration with TusServer ---------------------------------------------


class TestGCSStorageWithServer:
    def test_full_upload_via_server(self, gcs_client):
        """GCSStorage works as drop-in replacement in TusServer."""
        import importlib

        import resumable_upload.storage.gcs_storage as mod

        importlib.reload(mod)
        from resumable_upload import TusServer

        gcs_storage = mod.GCSStorage(
            bucket=TEST_BUCKET,
            gcs_client=gcs_client,
            prefix="srv",
            part_size=10,
        )
        gcs_storage._flush_size = 10  # Override for testing
        server = TusServer(storage=gcs_storage, base_path="/files")

        data = b"server integration test data"

        # Create
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            {"tus-resumable": "1.0.0", "upload-length": str(len(data))},
        )
        assert status == 201
        upload_id = headers["Location"].split("/")[-1]

        # PATCH
        status, headers, _ = server.handle_request(
            "PATCH",
            f"/files/{upload_id}",
            {
                "tus-resumable": "1.0.0",
                "upload-offset": "0",
                "content-type": "application/offset+octet-stream",
            },
            body=data,
        )
        assert status == 204
        assert headers["Upload-Offset"] == str(len(data))

        # HEAD
        status, headers, _ = server.handle_request(
            "HEAD",
            f"/files/{upload_id}",
            {"tus-resumable": "1.0.0"},
        )
        assert status == 200
        assert headers["Upload-Offset"] == str(len(data))

        # Read back
        gcs_storage.complete_upload(upload_id)
        assert gcs_storage.read_file(upload_id) == data

        # DELETE
        status, _, _ = server.handle_request(
            "DELETE",
            f"/files/{upload_id}",
            {"tus-resumable": "1.0.0"},
        )
        assert status == 204
        assert gcs_storage.get_upload(upload_id) is None
