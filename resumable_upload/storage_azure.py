"""Azure Blob Storage backend for resumable uploads.

Uses Azure Block Blobs with staged blocks to map TUS chunked uploads.
Metadata is stored as .info JSON blobs alongside the upload data.

Requires azure-storage-blob: pip install resumable-upload[azure]
"""

import base64
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from resumable_upload.storage import Storage

logger = logging.getLogger(__name__)

try:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import BlobBlock, BlobServiceClient
except ImportError as e:
    raise ImportError(
        "azure-storage-blob is required for AzureBlobStorage. "
        "Install it with: pip install resumable-upload[azure]"
    ) from e


class AzureBlobStorage(Storage):
    """Azure Blob Storage backend using staged blocks.

    Each TUS upload maps to:
    - {prefix}/{upload_id}.info    — JSON metadata
    - {prefix}/{upload_id}.buffer  — buffered data not yet staged as a block
    - Staged blocks on {prefix}/{upload_id} — uncommitted blocks

    After complete_upload() is called, the final blob lives at:
    - {prefix}/{upload_id}
    """

    _MIN_PART_SIZE = 5 * 1024 * 1024

    def __init__(
        self,
        container: str,
        connection_string: Optional[str] = None,
        container_client: Any = None,
        prefix: str = "",
        part_size: int = 8 * 1024 * 1024,
    ):
        """Initialize Azure Blob storage.

        Args:
            container: Azure Blob container name
            connection_string: Azure Storage connection string (uses env if None)
            container_client: Pre-configured ContainerClient (takes precedence)
            prefix: Key prefix for all blobs (no trailing slash)
            part_size: Target block size for staged uploads (default 8MB)
        """
        if container_client is not None:
            self.container_client = container_client
        elif connection_string:
            service = BlobServiceClient.from_connection_string(connection_string)
            self.container_client = service.get_container_client(container)
        else:
            import os

            conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
            service = BlobServiceClient.from_connection_string(conn_str)
            self.container_client = service.get_container_client(container)

        self.container_name = container
        self.prefix = prefix.strip("/")
        self.part_size = part_size
        self._flush_size = max(part_size, self._MIN_PART_SIZE)

    # -- Key helpers ---------------------------------------------------------

    def _info_key(self, upload_id: str) -> str:
        parts = [self.prefix, f"{upload_id}.info"]
        return "/".join(p for p in parts if p)

    def _buffer_key(self, upload_id: str) -> str:
        parts = [self.prefix, f"{upload_id}.buffer"]
        return "/".join(p for p in parts if p)

    def _object_key(self, upload_id: str) -> str:
        parts = [self.prefix, upload_id]
        return "/".join(p for p in parts if p)

    @staticmethod
    def _make_block_id(part_number: int) -> str:
        """Create a base64-encoded block ID with fixed length."""
        return base64.b64encode(f"{part_number:06d}".encode()).decode()

    # -- Blob helpers --------------------------------------------------------

    def _get_blob_client(self, key: str):
        return self.container_client.get_blob_client(key)

    def _upload_blob(self, key: str, data: bytes, content_type: Optional[str] = None) -> None:
        blob = self._get_blob_client(key)
        kwargs: dict[str, Any] = {"overwrite": True}
        if content_type:
            from azure.storage.blob import ContentSettings

            kwargs["content_settings"] = ContentSettings(content_type=content_type)
        blob.upload_blob(data, **kwargs)

    def _download_blob(self, key: str) -> bytes:
        blob = self._get_blob_client(key)
        data: bytes = blob.download_blob().readall()
        return data

    def _delete_blob(self, key: str) -> None:
        blob = self._get_blob_client(key)
        blob.delete_blob()

    # -- Info persistence ----------------------------------------------------

    def _read_info(self, upload_id: str) -> Optional[dict]:
        try:
            raw = self._download_blob(self._info_key(upload_id))
            result: dict = json.loads(raw)
            return result
        except ResourceNotFoundError:
            return None

    def _write_info(self, upload_id: str, info: dict) -> None:
        self._upload_blob(
            self._info_key(upload_id),
            json.dumps(info).encode(),
            content_type="application/json",
        )

    # -- Storage ABC implementation ------------------------------------------

    def create_upload(
        self,
        upload_id: str,
        upload_length: int,
        metadata: dict[str, str],
        expires_at: Optional[datetime] = None,
        is_partial: bool = False,
    ) -> None:
        info = {
            "upload_id": upload_id,
            "upload_length": upload_length,
            "offset": 0,
            "metadata": metadata,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "completed": False,
            "is_partial": is_partial,
            "blocks": [],
            "buffer_size": 0,
        }
        self._write_info(upload_id, info)

    def get_upload(self, upload_id: str) -> Optional[dict[str, Any]]:
        info = self._read_info(upload_id)
        if info is None:
            return None

        expires_at = None
        if info.get("expires_at"):
            try:
                dt = datetime.fromisoformat(info["expires_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                expires_at = dt
            except (ValueError, AttributeError):
                pass

        return {
            "upload_id": info["upload_id"],
            "upload_length": info["upload_length"],
            "offset": info["offset"],
            "metadata": info.get("metadata", {}),
            "completed": info.get("completed", False),
            "expires_at": expires_at,
            "is_partial": info.get("is_partial", False),
        }

    def update_offset(self, upload_id: str, offset: int) -> None:
        info = self._read_info(upload_id)
        if info is None:
            return
        info["offset"] = offset
        self._write_info(upload_id, info)

    def update_offset_atomic(self, upload_id: str, expected_offset: int, new_offset: int) -> bool:
        info = self._read_info(upload_id)
        if info is None or info["offset"] != expected_offset:
            return False
        info["offset"] = new_offset
        self._write_info(upload_id, info)
        return True

    def delete_upload(self, upload_id: str) -> None:
        info = self._read_info(upload_id)

        # If upload is in progress with staged blocks, upload an empty blob
        # to clear uncommitted blocks before deleting
        if info and info.get("blocks") and not info.get("completed"):
            with contextlib.suppress(ResourceNotFoundError):
                self._upload_blob(self._object_key(upload_id), b"")

        # Delete info, buffer, and final object
        for key in [
            self._info_key(upload_id),
            self._buffer_key(upload_id),
            self._object_key(upload_id),
        ]:
            with contextlib.suppress(ResourceNotFoundError):
                self._delete_blob(key)

    def write_chunk(self, upload_id: str, offset: int, data: bytes) -> None:
        info = self._read_info(upload_id)
        if info is None:
            raise ValueError(f"Upload {upload_id} not found")

        # Read existing buffer
        existing_buffer = b""
        if info["buffer_size"] > 0:
            try:
                existing_buffer = self._download_blob(self._buffer_key(upload_id))
            except ResourceNotFoundError:
                existing_buffer = b""

        # Append new data to buffer
        combined = existing_buffer + data

        # Stage blocks while we have enough data
        while len(combined) >= self._flush_size:
            block_data = combined[: self.part_size]
            combined = combined[self.part_size :]
            self._stage_block(upload_id, info, block_data)

        # Store remaining buffer
        if combined:
            self._upload_blob(self._buffer_key(upload_id), combined)
            info["buffer_size"] = len(combined)
        else:
            with contextlib.suppress(ResourceNotFoundError):
                self._delete_blob(self._buffer_key(upload_id))
            info["buffer_size"] = 0

        self._write_info(upload_id, info)

    def _stage_block(self, upload_id: str, info: dict, data: bytes) -> None:
        """Stage a single block on the target blob."""
        block_number = len(info["blocks"]) + 1
        block_id = self._make_block_id(block_number)
        blob = self._get_blob_client(self._object_key(upload_id))
        blob.stage_block(block_id=block_id, data=data)
        info["blocks"].append(
            {
                "block_id": block_id,
                "size": len(data),
            }
        )

    def complete_upload(self, upload_id: str) -> bool:
        """Finalize the upload by committing staged blocks.

        Strategy:
        - If blocks were staged: flush remaining buffer as last block,
          then commit_block_list to finalize.
        - If no blocks yet (all data in buffer): upload directly as final blob.
        """
        info = self._read_info(upload_id)
        if info is None:
            raise ValueError(f"Upload {upload_id} not found")
        if info.get("completed"):
            return False

        # Read remaining buffer
        remaining = b""
        if info["buffer_size"] > 0:
            try:
                remaining = self._download_blob(self._buffer_key(upload_id))
            except ResourceNotFoundError:
                raise ValueError(
                    f"Upload {upload_id}: buffer blob missing but "
                    f"buffer_size={info['buffer_size']}. "
                    "Cannot complete without buffered data."
                ) from None

        has_blocks = len(info["blocks"]) > 0

        if has_blocks:
            # Stage remaining buffer as last block
            if remaining:
                self._stage_block(upload_id, info, remaining)

            # Commit all blocks
            block_list = [BlobBlock(block_id=b["block_id"]) for b in info["blocks"]]
            blob = self._get_blob_client(self._object_key(upload_id))
            blob.commit_block_list(block_list)
        else:
            # No blocks — upload directly
            self._upload_blob(self._object_key(upload_id), remaining)

        # Clean up buffer
        with contextlib.suppress(ResourceNotFoundError):
            self._delete_blob(self._buffer_key(upload_id))

        info["completed"] = True
        info["blocks"] = []
        self._write_info(upload_id, info)
        return True

    def read_file(self, upload_id: str) -> bytes:
        try:
            return self._download_blob(self._object_key(upload_id))
        except ResourceNotFoundError:
            raise FileNotFoundError(f"Upload {upload_id} not found in Azure Blob Storage") from None

    def get_file_info(self, upload_id: str) -> dict[str, Any]:
        return {
            "upload_id": upload_id,
            "container": self.container_name,
            "key": self._object_key(upload_id),
        }

    def get_expired_uploads(self) -> list[str]:
        now = datetime.now(timezone.utc)
        expired = []

        prefix = f"{self.prefix}/" if self.prefix else ""
        blobs = self.container_client.list_blobs(name_starts_with=prefix)
        for blob in blobs:
            if not blob.name.endswith(".info"):
                continue
            try:
                data = self._download_blob(blob.name)
                info = json.loads(data)
                if info.get("expires_at"):
                    dt = datetime.fromisoformat(info["expires_at"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < now:
                        expired.append(info["upload_id"])
            except (ResourceNotFoundError, json.JSONDecodeError, KeyError):
                continue

        return expired

    def cleanup_expired_uploads(self) -> int:
        expired_ids = self.get_expired_uploads()
        for upload_id in expired_ids:
            self.delete_upload(upload_id)
        return len(expired_ids)

    def concatenate_uploads(
        self,
        final_id: str,
        partial_ids: list[str],
        metadata: dict[str, str],
    ) -> int:
        raise NotImplementedError(
            "Azure concatenation is not yet supported; planned for Phase B "
            "via staged blocks + commit_block_list. Use SQLiteStorage for "
            "local development or merge in a post-finish hook."
        )
