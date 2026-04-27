"""Tests for AzureBlobStorage backend using mock Azure client."""

from datetime import datetime, timedelta, timezone

import pytest


class FakeBlobClient:
    """In-memory blob client that mimics azure.storage.blob.BlobClient."""

    def __init__(self, container: "FakeContainerClient", name: str):
        self._container = container
        self.blob_name = name
        self._staged_blocks: dict[str, bytes] = {}

    def upload_blob(self, data, overwrite=False, content_settings=None):
        if isinstance(data, str):
            data = data.encode()
        self._container._blobs[self.blob_name] = data
        # Clear staged blocks on direct upload
        self._staged_blocks.clear()

    def download_blob(self):
        from azure.core.exceptions import ResourceNotFoundError

        if self.blob_name not in self._container._blobs:
            raise ResourceNotFoundError(f"Blob {self.blob_name} not found")
        data = self._container._blobs[self.blob_name]

        class FakeDownload:
            def __init__(self, content):
                self._content = content

            def readall(self):
                return self._content

        return FakeDownload(data)

    def delete_blob(self):
        from azure.core.exceptions import ResourceNotFoundError

        if self.blob_name not in self._container._blobs:
            raise ResourceNotFoundError(f"Blob {self.blob_name} not found")
        del self._container._blobs[self.blob_name]

    def stage_block(self, block_id, data):
        # Store staged block; also register in container's staged blocks for this blob
        if self.blob_name not in self._container._staged:
            self._container._staged[self.blob_name] = {}
        self._container._staged[self.blob_name][block_id] = data

    def commit_block_list(self, block_list):
        # Assemble blob from staged blocks
        staged = self._container._staged.get(self.blob_name, {})
        combined = b""
        for block in block_list:
            block_id = block.block_id if hasattr(block, "block_id") else block["block_id"]
            combined += staged[block_id]
        self._container._blobs[self.blob_name] = combined
        # Clear staged blocks
        self._container._staged.pop(self.blob_name, None)


class FakeBlobProperties:
    """Mimics blob properties returned by list_blobs."""

    def __init__(self, name: str):
        self.name = name


class FakeContainerClient:
    """In-memory container client that mimics azure.storage.blob.ContainerClient."""

    def __init__(self, name: str):
        self.container_name = name
        self._blobs: dict[str, bytes] = {}
        self._staged: dict[str, dict[str, bytes]] = {}
        self._blob_clients: dict[str, FakeBlobClient] = {}

    def get_blob_client(self, blob_name: str) -> FakeBlobClient:
        if blob_name not in self._blob_clients:
            self._blob_clients[blob_name] = FakeBlobClient(self, blob_name)
        return self._blob_clients[blob_name]

    def list_blobs(self, name_starts_with=""):
        result = []
        for name in sorted(self._blobs.keys()):
            if name.startswith(name_starts_with):
                result.append(FakeBlobProperties(name))
        return result


# Mock Azure imports before importing AzureBlobStorage
@pytest.fixture(autouse=True)
def mock_azure_imports():
    """Mock azure.storage.blob and azure.core.exceptions modules."""
    import sys
    import types

    class ResourceNotFoundError(Exception):
        pass

    class BlobBlock:
        def __init__(self, block_id):
            self.block_id = block_id

    class BlobServiceClient:
        @classmethod
        def from_connection_string(cls, conn_str):
            return cls()

        def get_container_client(self, container):
            return FakeContainerClient(container)

    class ContentSettings:
        def __init__(self, content_type=None):
            self.content_type = content_type

    # Build module hierarchy
    azure_mod = types.ModuleType("azure")
    core_mod = types.ModuleType("azure.core")
    core_exc_mod = types.ModuleType("azure.core.exceptions")
    core_exc_mod.ResourceNotFoundError = ResourceNotFoundError
    storage_mod = types.ModuleType("azure.storage")
    blob_mod = types.ModuleType("azure.storage.blob")
    blob_mod.BlobServiceClient = BlobServiceClient
    blob_mod.ContainerClient = FakeContainerClient
    blob_mod.BlobBlock = BlobBlock
    blob_mod.ContentSettings = ContentSettings

    azure_mod.core = core_mod
    azure_mod.storage = storage_mod
    core_mod.exceptions = core_exc_mod
    storage_mod.blob = blob_mod

    saved = {}
    mod_names = [
        "azure",
        "azure.core",
        "azure.core.exceptions",
        "azure.storage",
        "azure.storage.blob",
    ]
    for mod_name in mod_names:
        saved[mod_name] = sys.modules.get(mod_name)

    sys.modules["azure"] = azure_mod
    sys.modules["azure.core"] = core_mod
    sys.modules["azure.core.exceptions"] = core_exc_mod
    sys.modules["azure.storage"] = storage_mod
    sys.modules["azure.storage.blob"] = blob_mod

    yield ResourceNotFoundError

    for mod_name, original in saved.items():
        if original is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = original


TEST_CONTAINER = "test-tus-uploads"


@pytest.fixture
def container_client():
    return FakeContainerClient(TEST_CONTAINER)


@pytest.fixture
def storage(container_client):
    import importlib

    import resumable_upload.storage.azure_storage as mod

    importlib.reload(mod)
    s = mod.AzureBlobStorage(
        container=TEST_CONTAINER,
        container_client=container_client,
        prefix="uploads",
        part_size=10,  # Small for testing
    )
    s._flush_size = 10  # Override for testing (real Azure enforces 5MB minimum)
    return s


# -- Basic CRUD operations --------------------------------------------------


class TestAzureStorageCreate:
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


class TestAzureStorageWrite:
    def test_write_chunk_buffering(self, storage):
        """Chunks smaller than part_size are buffered."""
        storage.create_upload("u2", 20, {})
        storage.write_chunk("u2", 0, b"ABCDE")
        storage.update_offset("u2", 5)

        upload = storage.get_upload("u2")
        assert upload["offset"] == 5
        assert not upload["completed"]

    def test_write_single_chunk(self, storage):
        """Write data >= part_size stages a block."""
        data = b"A" * 20
        storage.create_upload("u1", len(data), {})
        storage.write_chunk("u1", 0, data[:10])
        storage.update_offset("u1", 10)

        upload = storage.get_upload("u1")
        assert upload["offset"] == 10

    def test_full_upload_flow(self, storage):
        """Complete upload with multiple chunks then read back data."""
        data = b"Hello, Azure Storage!"
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


class TestAzureStorageDelete:
    def test_delete_upload(self, storage):
        storage.create_upload("del-id", 100, {})
        assert storage.get_upload("del-id") is not None
        storage.delete_upload("del-id")
        assert storage.get_upload("del-id") is None

    def test_delete_nonexistent_upload(self, storage):
        """Deleting non-existent upload should not raise."""
        storage.delete_upload("nonexistent")


# -- Offset operations -------------------------------------------------------


class TestAzureStorageOffset:
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


class TestAzureStorageFileInfo:
    def test_get_file_info(self, storage):
        storage.create_upload("info-id", 100, {})
        info = storage.get_file_info("info-id")
        assert info["upload_id"] == "info-id"
        assert info["container"] == TEST_CONTAINER
        assert "key" in info

    def test_get_file_path_raises(self, storage):
        with pytest.raises(NotImplementedError):
            storage.get_file_path("any-id")


# -- Expiration --------------------------------------------------------------


class TestAzureStorageExpiration:
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


class TestAzureStorageWithServer:
    def test_full_upload_via_server(self, container_client):
        """AzureBlobStorage works as drop-in replacement in TusServer."""
        import importlib

        import resumable_upload.storage.azure_storage as mod

        importlib.reload(mod)
        from resumable_upload import TusServer

        azure_storage = mod.AzureBlobStorage(
            container=TEST_CONTAINER,
            container_client=container_client,
            prefix="srv",
            part_size=10,
        )
        azure_storage._flush_size = 10  # Override for testing
        server = TusServer(storage=azure_storage, base_path="/files")

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
        azure_storage.complete_upload(upload_id)
        assert azure_storage.read_file(upload_id) == data

        # DELETE
        status, _, _ = server.handle_request(
            "DELETE",
            f"/files/{upload_id}",
            {"tus-resumable": "1.0.0"},
        )
        assert status == 204
        assert azure_storage.get_upload(upload_id) is None
