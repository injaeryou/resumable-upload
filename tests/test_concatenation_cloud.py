"""Tests for TUS concatenation on cloud storage backends (S3 / GCS / Azure)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture
def s3_bucket():
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield "test-bucket", client


def _fill_partial(storage, upload_id: str, data: bytes) -> None:
    storage.create_upload(upload_id, len(data), {}, is_partial=True)
    storage.write_chunk(upload_id, 0, data)
    storage.update_offset(upload_id, len(data))
    storage.complete_upload(upload_id)


class TestS3Concatenation:
    # S3 UploadPartCopy requires each non-last part to be at least 5 MiB.
    # Keep partial sizes above that threshold so the implementation exercises
    # the UploadPartCopy path end-to-end.
    _PART_SIZE = 6 * 1024 * 1024

    def test_concatenate_two_partials(self, s3_bucket):
        bucket, client = s3_bucket
        from resumable_upload.storage.s3_storage import S3Storage

        storage = S3Storage(bucket=bucket, s3_client=client)

        a_data = b"A" * self._PART_SIZE
        b_data = b"B" * self._PART_SIZE
        _fill_partial(storage, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", a_data)
        _fill_partial(storage, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", b_data)

        final_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        total = storage.concatenate_uploads(
            final_id=final_id,
            partial_ids=[
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            ],
            metadata={"filename": "merged.bin"},
        )
        assert total == 2 * self._PART_SIZE

        final = storage.get_upload(final_id)
        assert final["upload_length"] == 2 * self._PART_SIZE
        assert final["offset"] == 2 * self._PART_SIZE
        assert final["completed"] is True
        assert final["is_partial"] is False
        assert final["concat_partial_ids"] == [
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ]
        assert storage.read_file(final_id) == a_data + b_data

    def test_concatenate_preserves_order(self, s3_bucket):
        bucket, client = s3_bucket
        from resumable_upload.storage.s3_storage import S3Storage

        storage = S3Storage(bucket=bucket, s3_client=client)

        a_data = b"A" * self._PART_SIZE
        b_data = b"B" * self._PART_SIZE
        _fill_partial(storage, "11111111-1111-1111-1111-111111111111", a_data)
        _fill_partial(storage, "22222222-2222-2222-2222-222222222222", b_data)

        final_id = "33333333-3333-3333-3333-333333333333"
        storage.concatenate_uploads(
            final_id=final_id,
            partial_ids=[
                "22222222-2222-2222-2222-222222222222",
                "11111111-1111-1111-1111-111111111111",
            ],
            metadata={},
        )
        assert storage.read_file(final_id) == b_data + a_data

    def test_concatenate_rejects_incomplete_partial(self, s3_bucket):
        bucket, client = s3_bucket
        from resumable_upload.storage.s3_storage import S3Storage

        storage = S3Storage(bucket=bucket, s3_client=client)

        pid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        storage.create_upload(pid, 100, {}, is_partial=True)
        storage.write_chunk(pid, 0, b"abc")
        storage.update_offset(pid, 3)  # incomplete

        with pytest.raises(ValueError, match="not complete"):
            storage.concatenate_uploads(
                final_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
                partial_ids=[pid],
                metadata={},
            )

    def test_concatenate_rejects_non_partial(self, s3_bucket):
        bucket, client = s3_bucket
        from resumable_upload.storage.s3_storage import S3Storage

        storage = S3Storage(bucket=bucket, s3_client=client)

        pid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        _fill_partial(storage, pid, b"ok")
        # Flip the flag via re-writing info so is_partial=False
        info = storage._read_info(pid)
        info["is_partial"] = False
        storage._write_info(pid, info)

        with pytest.raises(ValueError, match="not a partial"):
            storage.concatenate_uploads(
                final_id="99999999-9999-9999-9999-999999999999",
                partial_ids=[pid],
                metadata={},
            )

    def test_concatenate_missing_partial_raises(self, s3_bucket):
        bucket, client = s3_bucket
        from resumable_upload.storage.s3_storage import S3Storage

        storage = S3Storage(bucket=bucket, s3_client=client)
        with pytest.raises(ValueError, match="not found"):
            storage.concatenate_uploads(
                final_id="99999999-9999-9999-9999-999999999999",
                partial_ids=["00000000-0000-0000-0000-000000000000"],
                metadata={},
            )


class TestGCSConcatenation:
    """GCS compose via MagicMock — verifies call pattern, not live wire behavior."""

    def test_compose_called_with_sources_and_destination(self):
        pytest.importorskip("google.cloud.storage")
        from resumable_upload.storage.gcs_storage import GCSStorage

        # Build a mock client that behaves like google.cloud.storage.Client
        recorded = {}

        class FakeBlob:
            def __init__(self, name):
                self.name = name
                self._data = b""

            def upload_from_string(self, data, content_type=None):
                self._data = data if isinstance(data, bytes) else data.encode()

            def download_as_bytes(self):
                return self._data

            def compose(self, sources):
                recorded.setdefault("compose_calls", []).append([s.name for s in sources])
                self._data = b"".join(s._data for s in sources)

            def exists(self):
                return self._data != b"" or False

            def reload(self):
                pass

            @property
            def size(self):
                return len(self._data)

        class FakeBucket:
            def __init__(self):
                self._blobs = {}

            def blob(self, name):
                return self._blobs.setdefault(name, FakeBlob(name))

        fake_bucket = FakeBucket()

        class FakeClient:
            def bucket(self, _name):
                return fake_bucket

            def get_bucket(self, _name):
                return fake_bucket

        storage = GCSStorage(bucket="test", gcs_client=FakeClient())

        # Manually seed info + blobs for two complete partials
        a_info = {
            "upload_id": "a",
            "upload_length": 5,
            "offset": 5,
            "metadata": {},
            "created_at": "2026-04-17T00:00:00+00:00",
            "expires_at": None,
            "completed": True,
            "is_partial": True,
            "parts": [],
            "buffer_size": 0,
        }
        b_info = dict(a_info, upload_id="b", upload_length=6, offset=6)

        # Persist info via the storage API
        storage._write_info("a", a_info)
        storage._write_info("b", b_info)
        # Simulate completed blobs at object_key paths
        fake_bucket.blob(storage._object_key("a"))._data = b"hello"
        fake_bucket.blob(storage._object_key("b"))._data = b"-world"

        total = storage.concatenate_uploads(
            final_id="final", partial_ids=["a", "b"], metadata={"filename": "f"}
        )
        assert total == 11
        final_blob = fake_bucket.blob(storage._object_key("final"))
        assert final_blob._data == b"hello-world"
        # compose was invoked at least once (maybe chained for >32)
        assert "compose_calls" in recorded


class TestAzureConcatenation:
    """Azure stage_block + commit_block_list via MagicMock."""

    def test_commit_block_list_after_staging(self):
        pytest.importorskip("azure.storage.blob")
        from resumable_upload.storage.azure_storage import AzureBlobStorage

        staged_blocks: dict[str, bytes] = {}
        committed: list[list] = []

        class FakeBlobClient:
            def __init__(self, name):
                self.name = name
                self._data = b""

            def upload_blob(self, data, overwrite=True, **_kwargs):
                # Real Azure SDK accepts content_settings, length, etc. — ignore all.
                self._data = data if isinstance(data, bytes) else data.encode()

            def download_blob(self):
                mock = MagicMock()
                mock.readall.return_value = self._data
                return mock

            def stage_block(self, block_id, data):
                staged_blocks[block_id] = data

            def commit_block_list(self, block_list, **kwargs):
                ordered = [staged_blocks[b.id] for b in block_list]
                committed.append(list(block_list))
                self._data = b"".join(ordered)

            def delete_blob(self, **kwargs):
                self._data = b""

            def exists(self):
                return True

        blobs: dict[str, FakeBlobClient] = {}

        class FakeContainerClient:
            def get_blob_client(self, blob_name):
                return blobs.setdefault(blob_name, FakeBlobClient(blob_name))

            def list_blobs(self, **kwargs):
                return []

        storage = AzureBlobStorage(container="c", container_client=FakeContainerClient())

        a_info = {
            "upload_id": "a",
            "upload_length": 5,
            "offset": 5,
            "metadata": {},
            "created_at": "2026-04-17T00:00:00+00:00",
            "expires_at": None,
            "completed": True,
            "is_partial": True,
            "blocks": [],
            "buffer_size": 0,
        }
        b_info = dict(a_info, upload_id="b", upload_length=6, offset=6)

        storage._write_info("a", a_info)
        storage._write_info("b", b_info)
        storage.container_client.get_blob_client(storage._object_key("a"))._data = b"hello"
        storage.container_client.get_blob_client(storage._object_key("b"))._data = b"-world"

        total = storage.concatenate_uploads(final_id="final", partial_ids=["a", "b"], metadata={})
        assert total == 11
        final_blob = storage.container_client.get_blob_client(storage._object_key("final"))
        assert final_blob._data == b"hello-world"
        assert committed, "commit_block_list must have been called"
