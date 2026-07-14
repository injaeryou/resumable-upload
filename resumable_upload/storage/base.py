"""Storage abstract base class."""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class Storage(ABC):
    """Abstract base class for storage backends.

    Sync methods are the canonical surface every backend must implement.
    Each I/O-bound sync method has a ``*_async`` sibling defined on this
    class with a default implementation that offloads the sync call to a
    worker thread via :func:`asyncio.to_thread`. ``TusASGIApp`` always
    awaits the async siblings, so true-async backends can override the
    specific methods they have native non-blocking implementations for
    and inherit the rest. See ``.docs/plans/2026-04-29-async-native-storage.md``.
    """

    #: True when the backend implements the TUS ``concatenation-unfinished``
    #: extension: ``concatenate_uploads(..., allow_unfinished=True)`` creates a
    #: pending final over incomplete partials, and :meth:`try_assemble_final` /
    #: :meth:`find_pending_finals_for_partial` drive its later assembly. The
    #: server only advertises the extension when this is True.
    supports_unfinished_concat: bool = False

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
    ) -> Optional[int]:
        """Create a final upload by concatenating completed partial uploads.

        Implementations should persist ``partial_ids`` on the final upload
        record and surface them from :meth:`get_upload` under the optional
        ``concat_partial_ids`` key, so the server can echo
        ``Upload-Concat: final;<urls>`` on HEAD. Backends that omit the key
        simply skip that echo (back-compat).

        Args:
            final_id: UUID for the new final upload.
            partial_ids: Ordered list of partial upload IDs to merge.
            metadata: Metadata for the resulting final upload.
            expires_at: Optional expiry timestamp to record on the final
                upload so the expiration extension applies to merged objects
                just like it does to ordinary uploads.

        Returns:
            Total byte length of the concatenated upload, or ``None`` when the
            backend supports ``concatenation-unfinished``, was called with
            ``allow_unfinished=True``, and some partial is still incomplete
            (the final stays *pending* until :meth:`try_assemble_final`).

        Raises:
            ValueError: If any partial is missing, not flagged is_partial,
                or not fully uploaded (and unfinished concat not allowed).
            NotImplementedError: If the backend does not support concatenation.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support the TUS concatenation extension"
        )

    def find_pending_finals_for_partial(self, partial_id: str) -> list[str]:
        """Return ids of pending (unassembled) final uploads referencing ``partial_id``.

        Only meaningful for backends with ``supports_unfinished_concat``.
        The default returns an empty list so the PATCH handler's assembly
        trigger is a no-op on backends without the extension.
        """
        return []

    def try_assemble_final(
        self, final_id: str, *, max_total: Optional[int] = None
    ) -> Optional[int]:
        """Assemble a pending final upload if all its partials are complete.

        Returns the total byte length when assembly happened, or ``None``
        when the final is still pending (some partial incomplete/missing) or
        was already assembled by a concurrent request. Implementations must
        make the assembly claim atomic so concurrent callers assemble at
        most once.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support the TUS concatenation-unfinished extension"
        )

    # ------------------------------------------------------------------
    # Async I/O surface (default = thread offload)
    #
    # Each method below is a thin ``await asyncio.to_thread(...)`` wrapper
    # around its sync sibling. True-async backends override these with
    # native non-blocking implementations; sync-only backends inherit the
    # defaults and gain async behavior for free.
    # ------------------------------------------------------------------

    async def create_upload_async(
        self,
        upload_id: str,
        upload_length: Optional[int],
        metadata: dict[str, str],
        expires_at: Optional[datetime] = None,
        is_partial: bool = False,
    ) -> None:
        await asyncio.to_thread(
            self.create_upload,
            upload_id,
            upload_length,
            metadata,
            expires_at,
            is_partial,
        )

    async def set_upload_length_async(self, upload_id: str, upload_length: int) -> None:
        await asyncio.to_thread(self.set_upload_length, upload_id, upload_length)

    async def get_upload_async(self, upload_id: str) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self.get_upload, upload_id)

    async def update_offset_async(self, upload_id: str, offset: int) -> None:
        await asyncio.to_thread(self.update_offset, upload_id, offset)

    async def update_offset_atomic_async(
        self, upload_id: str, expected_offset: int, new_offset: int
    ) -> bool:
        return await asyncio.to_thread(
            self.update_offset_atomic, upload_id, expected_offset, new_offset
        )

    async def complete_upload_async(self, upload_id: str) -> bool:
        return await asyncio.to_thread(self.complete_upload, upload_id)

    async def delete_upload_async(self, upload_id: str) -> None:
        await asyncio.to_thread(self.delete_upload, upload_id)

    async def write_chunk_async(self, upload_id: str, offset: int, data: bytes) -> None:
        await asyncio.to_thread(self.write_chunk, upload_id, offset, data)

    async def read_file_async(self, upload_id: str) -> bytes:
        return await asyncio.to_thread(self.read_file, upload_id)

    async def get_expired_uploads_async(self) -> list[str]:
        return await asyncio.to_thread(self.get_expired_uploads)

    async def cleanup_expired_uploads_async(self) -> int:
        return await asyncio.to_thread(self.cleanup_expired_uploads)

    async def concatenate_uploads_async(
        self,
        final_id: str,
        partial_ids: list[str],
        metadata: dict[str, str],
        *,
        expires_at: Optional[datetime] = None,
        **kwargs: Any,
    ) -> Optional[int]:
        return await asyncio.to_thread(
            lambda: self.concatenate_uploads(
                final_id,
                partial_ids,
                metadata,
                expires_at=expires_at,
                **kwargs,
            )
        )

    async def find_pending_finals_for_partial_async(self, partial_id: str) -> list[str]:
        return await asyncio.to_thread(self.find_pending_finals_for_partial, partial_id)

    async def try_assemble_final_async(
        self, final_id: str, *, max_total: Optional[int] = None
    ) -> Optional[int]:
        return await asyncio.to_thread(
            lambda: self.try_assemble_final(final_id, max_total=max_total)
        )

    async def get_file_info_async(self, upload_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.get_file_info, upload_id)
