"""Tests for the TUS concatenation extension at the storage layer."""

import os
import shutil
import tempfile

import pytest

from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def storage():
    temp_dir = tempfile.mkdtemp()
    try:
        yield SQLiteStorage(
            db_path=os.path.join(temp_dir, "u.db"),
            upload_dir=os.path.join(temp_dir, "files"),
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _fill(storage: SQLiteStorage, upload_id: str, data: bytes, *, is_partial: bool) -> None:
    """Create an upload, write the full content, and mark it completed."""
    storage.create_upload(upload_id, len(data), {}, is_partial=is_partial)
    storage.write_chunk(upload_id, 0, data)
    storage.update_offset(upload_id, len(data))
    storage.complete_upload(upload_id)


class TestConcatenation:
    def test_concatenate_two_partials(self, storage):
        p1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        p2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        _fill(storage, p1, b"hello", is_partial=True)
        _fill(storage, p2, b"-world", is_partial=True)

        final_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        total = storage.concatenate_uploads(
            final_id=final_id,
            partial_ids=[p1, p2],
            metadata={"filename": "merged.txt"},
        )

        assert total == 11
        final = storage.get_upload(final_id)
        assert final["upload_length"] == 11
        assert final["offset"] == 11
        assert final["completed"] is True
        assert final["is_partial"] is False
        assert final["metadata"] == {"filename": "merged.txt"}
        assert storage.read_file(final_id) == b"hello-world"

    def test_concatenate_preserves_partial_order(self, storage):
        # Reverse order should reverse the concatenation.
        p1 = "11111111-1111-1111-1111-111111111111"
        p2 = "22222222-2222-2222-2222-222222222222"
        _fill(storage, p1, b"first", is_partial=True)
        _fill(storage, p2, b"second", is_partial=True)

        final_id = "33333333-3333-3333-3333-333333333333"
        total = storage.concatenate_uploads(final_id=final_id, partial_ids=[p2, p1], metadata={})
        assert total == 11
        assert storage.read_file(final_id) == b"secondfirst"

    def test_concatenate_rejects_incomplete_partial(self, storage):
        p = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        storage.create_upload(p, 10, {}, is_partial=True)
        storage.write_chunk(p, 0, b"abc")
        storage.update_offset(p, 3)  # offset < length → incomplete

        with pytest.raises(ValueError, match="not complete"):
            storage.concatenate_uploads(
                final_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
                partial_ids=[p],
                metadata={},
            )
        # No final upload should have been created
        assert storage.get_upload("dddddddd-dddd-dddd-dddd-dddddddddddd") is None

    def test_concatenate_rejects_non_partial(self, storage):
        p = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        _fill(storage, p, b"hello", is_partial=False)

        with pytest.raises(ValueError, match="not a partial"):
            storage.concatenate_uploads(
                final_id="99999999-9999-9999-9999-999999999999",
                partial_ids=[p],
                metadata={},
            )

    def test_concatenate_rejects_missing_partial(self, storage):
        with pytest.raises(ValueError, match="not found"):
            storage.concatenate_uploads(
                final_id="99999999-9999-9999-9999-999999999999",
                partial_ids=["00000000-0000-0000-0000-000000000000"],
                metadata={},
            )

    def test_concatenate_many_partials_streams_correctly(self, storage):
        """Exercise the stream loop with more data than a single read buffer."""
        # 3 MB total, well above the 1 MB internal copy buffer
        chunk = b"x" * (512 * 1024)
        ids = []
        for i in range(6):
            pid = f"{i:08x}-1111-1111-1111-111111111111"
            _fill(storage, pid, chunk, is_partial=True)
            ids.append(pid)

        final_id = "55555555-5555-5555-5555-555555555555"
        total = storage.concatenate_uploads(final_id=final_id, partial_ids=ids, metadata={})
        assert total == len(chunk) * 6
        assert storage.read_file(final_id) == chunk * 6


class TestConcatenationNotImplemented:
    """Concatenation on cloud backends is deferred — they must raise explicitly."""

    def test_s3_raises_not_implemented(self):
        pytest.importorskip("boto3")
        from unittest.mock import MagicMock

        from resumable_upload.storage_s3 import S3Storage

        storage = S3Storage(bucket="b", s3_client=MagicMock())
        with pytest.raises(NotImplementedError, match="concatenation"):
            storage.concatenate_uploads(final_id="x", partial_ids=["y"], metadata={})

    def test_gcs_raises_not_implemented(self):
        pytest.importorskip("google.cloud.storage")
        from unittest.mock import MagicMock

        from resumable_upload.storage_gcs import GCSStorage

        storage = GCSStorage(bucket="b", gcs_client=MagicMock())
        with pytest.raises(NotImplementedError, match="concatenation"):
            storage.concatenate_uploads(final_id="x", partial_ids=["y"], metadata={})

    def test_azure_raises_not_implemented(self):
        pytest.importorskip("azure.storage.blob")
        from unittest.mock import MagicMock

        from resumable_upload.storage_azure import AzureBlobStorage

        storage = AzureBlobStorage(container="c", container_client=MagicMock())
        with pytest.raises(NotImplementedError, match="concatenation"):
            storage.concatenate_uploads(final_id="x", partial_ids=["y"], metadata={})
