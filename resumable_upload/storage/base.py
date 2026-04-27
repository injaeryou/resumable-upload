"""Storage abstract base class."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class Storage(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def create_upload(
        self,
        upload_id: str,
        upload_length: Optional[int],
        metadata: dict[str, str],
        expires_at: Optional[datetime] = None,
        is_partial: bool = False,
    ) -> None:
        """Create a new upload entry.

        Args:
            upload_length: Total byte size. ``None`` marks the upload as
                deferred-length (Upload-Defer-Length extension); the final
                length is committed on the first PATCH via
                :meth:`set_upload_length`.
            is_partial: If True, this upload is a partial upload that will be
                consumed by a final concatenation request. Partial uploads are
                never delivered to the on_upload_complete hook individually.
        """
        pass

    def set_upload_length(self, upload_id: str, upload_length: int) -> None:
        """Commit the final length of a deferred-length upload.

        Default implementation raises; concrete backends override.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support deferred-length uploads")

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

    def concatenate_uploads(
        self,
        final_id: str,
        partial_ids: list[str],
        metadata: dict[str, str],
        *,
        expires_at: Optional[datetime] = None,
    ) -> int:
        """Create a final upload by concatenating completed partial uploads.

        Args:
            final_id: UUID for the new final upload.
            partial_ids: Ordered list of partial upload IDs to merge.
            metadata: Metadata for the resulting final upload.
            expires_at: Optional expiry timestamp to record on the final
                upload so the expiration extension applies to merged objects
                just like it does to ordinary uploads.

        Returns:
            Total byte length of the concatenated upload.

        Raises:
            ValueError: If any partial is missing, not flagged is_partial,
                or not fully uploaded.
            NotImplementedError: If the backend does not support concatenation.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support the TUS concatenation extension"
        )
