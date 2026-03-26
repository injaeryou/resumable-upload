"""Tests for S3Storage backend."""

from datetime import datetime, timedelta, timezone

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from resumable_upload.storage_s3 import S3Storage  # noqa: E402

TEST_BUCKET = "test-tus-uploads"
TEST_REGION = "us-east-1"


@pytest.fixture
def s3_client():
    """Create a mocked S3 client with a test bucket."""
    with mock_aws():
        client = boto3.client("s3", region_name=TEST_REGION)
        client.create_bucket(Bucket=TEST_BUCKET)
        yield client


@pytest.fixture
def storage(s3_client):
    """Create an S3Storage with mocked S3."""
    return S3Storage(
        bucket=TEST_BUCKET,
        s3_client=s3_client,
        prefix="uploads",
        part_size=5 * 1024 * 1024,  # 5MB minimum
    )


@pytest.fixture
def small_storage(s3_client):
    """S3Storage with small part_size for easier testing of multipart."""
    return S3Storage(
        bucket=TEST_BUCKET,
        s3_client=s3_client,
        prefix="uploads",
        part_size=10,  # Very small for testing (real S3 requires 5MB)
    )


# -- Basic CRUD operations --------------------------------------------------

class TestS3StorageCreate:
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


class TestS3StorageWrite:
    def test_write_single_chunk_completes_upload(self, small_storage):
        """Write data >= part_size → flushes as S3 part immediately."""
        data = b"A" * 20  # 20 bytes > part_size of 10
        small_storage.create_upload("u1", len(data), {})
        small_storage.write_chunk("u1", 0, data[:10])
        small_storage.update_offset("u1", 10)

        upload = small_storage.get_upload("u1")
        assert upload["offset"] == 10

    def test_write_chunk_buffering(self, small_storage):
        """Chunks smaller than part_size are buffered."""
        small_storage.create_upload("u2", 20, {})
        small_storage.write_chunk("u2", 0, b"ABCDE")  # 5 bytes < 10 part_size
        small_storage.update_offset("u2", 5)

        upload = small_storage.get_upload("u2")
        assert upload["offset"] == 5
        assert not upload["completed"]

    def test_full_upload_flow(self, small_storage):
        """Complete upload with multiple chunks → read back data."""
        data = b"Hello, S3 Storage!"
        small_storage.create_upload("full", len(data), {"filename": "test.txt"})

        offset = 0
        chunk_size = 10
        while offset < len(data):
            chunk = data[offset:offset + chunk_size]
            small_storage.write_chunk("full", offset, chunk)
            offset += len(chunk)
            small_storage.update_offset("full", offset)

        # Finalize the upload
        small_storage.complete_upload("full")

        upload = small_storage.get_upload("full")
        assert upload["completed"] is True

        result = small_storage.read_file("full")
        assert result == data

    def test_single_shot_upload(self, small_storage):
        """Upload where all data comes in one chunk."""
        data = b"one shot"
        small_storage.create_upload("oneshot", len(data), {})
        small_storage.write_chunk("oneshot", 0, data)
        small_storage.update_offset("oneshot", len(data))
        small_storage.complete_upload("oneshot")

        assert small_storage.read_file("oneshot") == data


class TestS3StorageDelete:
    def test_delete_upload(self, storage):
        storage.create_upload("del-id", 100, {})
        assert storage.get_upload("del-id") is not None
        storage.delete_upload("del-id")
        assert storage.get_upload("del-id") is None

    def test_delete_nonexistent_upload(self, storage):
        """Deleting non-existent upload should not raise."""
        storage.delete_upload("nonexistent")


# -- Offset operations -------------------------------------------------------

class TestS3StorageOffset:
    def test_update_offset(self, storage):
        storage.create_upload("off-id", 100, {})
        storage.update_offset("off-id", 50)
        upload = storage.get_upload("off-id")
        assert upload["offset"] == 50
        assert not upload["completed"]

    def test_update_offset_marks_completed(self, storage):
        storage.create_upload("comp-id", 100, {})
        storage.update_offset("comp-id", 100)
        upload = storage.get_upload("comp-id")
        assert upload["completed"] is True

    def test_update_offset_atomic_success(self, storage):
        storage.create_upload("atom-id", 100, {})
        assert storage.update_offset_atomic("atom-id", 0, 50) is True
        upload = storage.get_upload("atom-id")
        assert upload["offset"] == 50

    def test_update_offset_atomic_conflict(self, storage):
        storage.create_upload("conflict-id", 100, {})
        storage.update_offset("conflict-id", 30)
        # Expected offset is 0 but actual is 30 → conflict
        assert storage.update_offset_atomic("conflict-id", 0, 50) is False
        # Offset unchanged
        upload = storage.get_upload("conflict-id")
        assert upload["offset"] == 30


# -- File info ---------------------------------------------------------------

class TestS3StorageFileInfo:
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

class TestS3StorageExpiration:
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

class TestS3StorageWithServer:
    def test_full_upload_via_server(self, s3_client):
        """S3Storage works as drop-in replacement for SQLiteStorage in TusServer."""
        from resumable_upload import TusServer

        storage = S3Storage(
            bucket=TEST_BUCKET,
            s3_client=s3_client,
            prefix="srv",
            part_size=10,
        )
        server = TusServer(storage=storage, base_path="/files")

        data = b"server integration test data"

        # Create
        status, headers, _ = server.handle_request(
            "POST", "/files",
            {"tus-resumable": "1.0.0", "upload-length": str(len(data))},
        )
        assert status == 201
        upload_id = headers["Location"].split("/")[-1]

        # PATCH
        status, headers, _ = server.handle_request(
            "PATCH", f"/files/{upload_id}",
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
            "HEAD", f"/files/{upload_id}",
            {"tus-resumable": "1.0.0"},
        )
        assert status == 200
        assert headers["Upload-Offset"] == str(len(data))

        # Read back data
        storage.complete_upload(upload_id)
        assert storage.read_file(upload_id) == data

        # DELETE
        status, _, _ = server.handle_request(
            "DELETE", f"/files/{upload_id}",
            {"tus-resumable": "1.0.0"},
        )
        assert status == 204
        assert storage.get_upload(upload_id) is None
