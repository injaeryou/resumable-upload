"""A *true-async* storage backend, served over ASGI.

Every storage backend inherits ``*_async`` methods that default to
``asyncio.to_thread(sync_method)`` — correct, but the blocking call still
ties up a worker thread. A backend with a native async client (aiofiles,
aioboto3, asyncpg, …) should instead **override** the ``*_async`` siblings so
``handle_request_async`` awaits real non-blocking I/O end to end.

This example is an in-memory store that does exactly that. There is no real
I/O to await, so the async methods ``await asyncio.sleep(0)`` where a real
backend would ``await client.put_object(...)`` — the point is the shape:
``handle_request_async`` -> ``TusServer`` -> these ``*_async`` overrides,
with the synchronous methods never touched on the async path.

``tests/test_storage_async_native.py`` pins that contract (it forbids the
``to_thread`` fallback and still completes a full upload).

Run::

    pip install uvicorn
    python examples/server/async_storage.py        # -> :8000
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

from resumable_upload.storage import Storage


class AsyncDictStorage(Storage):
    """In-memory store whose ``*_async`` methods are natively async.

    Sync and async methods share private ``_do_*`` helpers so the protocol
    logic lives once; only the async methods ``await``. Swap the
    ``asyncio.sleep(0)`` calls for real ``await``-ing I/O in a production
    backend.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._blobs: dict[str, bytearray] = {}

    # -- shared logic (no I/O) -------------------------------------------

    def _do_create(
        self,
        upload_id: str,
        upload_length: int | None,
        metadata: dict[str, str],
        expires_at: datetime | None,
        is_partial: bool,
    ) -> None:
        self._records[upload_id] = {
            "upload_id": upload_id,
            "upload_length": upload_length,
            "offset": 0,
            "metadata": dict(metadata),
            "completed": False,
            "expires_at": expires_at,
            "is_partial": is_partial,
        }
        self._blobs[upload_id] = bytearray()

    def _do_get(self, upload_id: str) -> dict[str, Any] | None:
        rec = self._records.get(upload_id)
        if rec is None:
            return None
        # Return a copy so callers never mutate our internal state.
        return {**rec, "metadata": dict(rec["metadata"])}

    def _do_set_length(self, upload_id: str, upload_length: int) -> None:
        self._records[upload_id]["upload_length"] = upload_length

    def _do_update_offset(self, upload_id: str, offset: int) -> None:
        self._records[upload_id]["offset"] = offset

    def _do_atomic(self, upload_id: str, expected: int, new: int) -> bool:
        rec = self._records.get(upload_id)
        if rec is None or rec["offset"] != expected:
            return False
        rec["offset"] = new
        return True

    def _do_complete(self, upload_id: str) -> bool:
        rec = self._records.get(upload_id)
        if rec is None or rec["completed"]:
            return False
        rec["completed"] = True
        return True

    def _do_delete(self, upload_id: str) -> None:
        self._records.pop(upload_id, None)
        self._blobs.pop(upload_id, None)

    def _do_write(self, upload_id: str, offset: int, data: bytes) -> None:
        blob = self._blobs[upload_id]
        del blob[offset:]
        blob.extend(data)

    def _do_read(self, upload_id: str) -> bytes:
        return bytes(self._blobs[upload_id])

    def _do_expired(self) -> list[str]:
        now = datetime.now(timezone.utc)
        return [
            uid
            for uid, rec in self._records.items()
            if rec["expires_at"] is not None and rec["expires_at"] < now
        ]

    def _do_cleanup(self) -> int:
        expired = self._do_expired()
        for uid in expired:
            self._do_delete(uid)
        return len(expired)

    # -- synchronous surface (required by the ABC) -----------------------

    def create_upload(
        self,
        upload_id: str,
        upload_length: int | None,
        metadata: dict[str, str],
        expires_at: datetime | None = None,
        is_partial: bool = False,
    ) -> None:
        self._do_create(upload_id, upload_length, metadata, expires_at, is_partial)

    def set_upload_length(self, upload_id: str, upload_length: int) -> None:
        self._do_set_length(upload_id, upload_length)

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        return self._do_get(upload_id)

    def update_offset(self, upload_id: str, offset: int) -> None:
        self._do_update_offset(upload_id, offset)

    def update_offset_atomic(self, upload_id: str, expected_offset: int, new_offset: int) -> bool:
        return self._do_atomic(upload_id, expected_offset, new_offset)

    def complete_upload(self, upload_id: str) -> bool:
        return self._do_complete(upload_id)

    def delete_upload(self, upload_id: str) -> None:
        self._do_delete(upload_id)

    def write_chunk(self, upload_id: str, offset: int, data: bytes) -> None:
        self._do_write(upload_id, offset, data)

    def read_file(self, upload_id: str) -> bytes:
        return self._do_read(upload_id)

    def get_expired_uploads(self) -> list[str]:
        return self._do_expired()

    def cleanup_expired_uploads(self) -> int:
        return self._do_cleanup()

    # -- native async surface (the whole point) --------------------------
    # Each awaits where a real backend would hit the network/disk, then runs
    # the shared logic. None of them call the synchronous methods, so the
    # ``to_thread`` fallback on Storage is never used on the async path.

    async def create_upload_async(
        self,
        upload_id: str,
        upload_length: int | None,
        metadata: dict[str, str],
        expires_at: datetime | None = None,
        is_partial: bool = False,
    ) -> None:
        await asyncio.sleep(0)
        self._do_create(upload_id, upload_length, metadata, expires_at, is_partial)

    async def set_upload_length_async(self, upload_id: str, upload_length: int) -> None:
        await asyncio.sleep(0)
        self._do_set_length(upload_id, upload_length)

    async def get_upload_async(self, upload_id: str) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return self._do_get(upload_id)

    async def update_offset_async(self, upload_id: str, offset: int) -> None:
        await asyncio.sleep(0)
        self._do_update_offset(upload_id, offset)

    async def update_offset_atomic_async(
        self, upload_id: str, expected_offset: int, new_offset: int
    ) -> bool:
        await asyncio.sleep(0)
        return self._do_atomic(upload_id, expected_offset, new_offset)

    async def complete_upload_async(self, upload_id: str) -> bool:
        await asyncio.sleep(0)
        return self._do_complete(upload_id)

    async def delete_upload_async(self, upload_id: str) -> None:
        await asyncio.sleep(0)
        self._do_delete(upload_id)

    async def write_chunk_async(self, upload_id: str, offset: int, data: bytes) -> None:
        await asyncio.sleep(0)
        self._do_write(upload_id, offset, data)

    async def read_file_async(self, upload_id: str) -> bytes:
        await asyncio.sleep(0)
        return self._do_read(upload_id)

    async def get_expired_uploads_async(self) -> list[str]:
        await asyncio.sleep(0)
        return self._do_expired()

    async def cleanup_expired_uploads_async(self) -> int:
        await asyncio.sleep(0)
        return self._do_cleanup()


def build_app() -> Any:
    """Build the ASGI app (importable without uvicorn installed)."""
    from resumable_upload import TusServer
    from resumable_upload.asgi import TusASGIApp

    tus = TusServer(
        storage=AsyncDictStorage(),
        base_path="/files",
        max_size=100 * 1024 * 1024,
        cors_allow_origins="*",
    )
    return TusASGIApp(tus)


def main() -> None:
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(build_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
