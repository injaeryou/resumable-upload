"""Async surface contract tests for Storage ABC.

Every I/O-bound sync method on Storage has a `*_async` sibling. The
default implementation simply offloads the sync call to a worker thread
via `asyncio.to_thread`, so:

1. The async sibling must produce the exact same result as the sync
   sibling for the same inputs.
2. The async sibling must invoke the sync sibling exactly once per
   await (verified with ``mock.patch.object(..., wraps=...)``).

True-async backends (e.g. a future S3AsyncStorage) are free to override
specific `*_async` methods; this contract exercises only the default
``to_thread`` wrappers via SQLiteStorage.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from resumable_upload.storage.sqlite_storage import SQLiteStorage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def storage(tmp_path):
    return SQLiteStorage(
        db_path=str(tmp_path / "uploads.db"),
        upload_dir=str(tmp_path / "files"),
    )


def _new_id() -> str:
    return str(uuid.uuid4())


@pytest.mark.anyio
async def test_create_upload_async_delegates_and_matches(storage):
    upload_id = _new_id()
    with patch.object(
        storage, "create_upload", wraps=storage.create_upload
    ) as spy:
        await storage.create_upload_async(upload_id, 5, {"filename": "x"})
    spy.assert_called_once()
    args, _ = spy.call_args
    assert args[:3] == (upload_id, 5, {"filename": "x"})
    assert storage.get_upload(upload_id)["upload_length"] == 5


@pytest.mark.anyio
async def test_get_upload_async_matches_sync(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, 3, {})
    with patch.object(storage, "get_upload", wraps=storage.get_upload) as spy:
        result = await storage.get_upload_async(upload_id)
    spy.assert_called_once_with(upload_id)
    assert result == storage.get_upload(upload_id)


@pytest.mark.anyio
async def test_update_offset_async_persists(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, 10, {})
    with patch.object(
        storage, "update_offset", wraps=storage.update_offset
    ) as spy:
        await storage.update_offset_async(upload_id, 4)
    spy.assert_called_once_with(upload_id, 4)
    assert storage.get_upload(upload_id)["offset"] == 4


@pytest.mark.anyio
async def test_update_offset_atomic_async_returns_bool(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, 10, {})
    storage.update_offset(upload_id, 0)
    with patch.object(
        storage, "update_offset_atomic", wraps=storage.update_offset_atomic
    ) as spy:
        ok = await storage.update_offset_atomic_async(upload_id, 0, 5)
        stale = await storage.update_offset_atomic_async(upload_id, 0, 7)
    assert ok is True
    assert stale is False
    assert spy.call_count == 2
    assert storage.get_upload(upload_id)["offset"] == 5


@pytest.mark.anyio
async def test_complete_upload_async_delegates(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, 3, {})
    storage.write_chunk(upload_id, 0, b"abc")
    storage.update_offset(upload_id, 3)
    with patch.object(
        storage, "complete_upload", wraps=storage.complete_upload
    ) as spy:
        result = await storage.complete_upload_async(upload_id)
    spy.assert_called_once_with(upload_id)
    # SQLiteStorage always returns True; this contract is per-backend.
    assert result is True
    assert storage.get_upload(upload_id)["completed"] is True


@pytest.mark.anyio
async def test_delete_upload_async_removes_record(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, 3, {})
    with patch.object(
        storage, "delete_upload", wraps=storage.delete_upload
    ) as spy:
        await storage.delete_upload_async(upload_id)
    spy.assert_called_once_with(upload_id)
    assert storage.get_upload(upload_id) is None


@pytest.mark.anyio
async def test_write_chunk_async_persists_bytes(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, 5, {})
    with patch.object(
        storage, "write_chunk", wraps=storage.write_chunk
    ) as spy:
        await storage.write_chunk_async(upload_id, 0, b"hello")
    spy.assert_called_once_with(upload_id, 0, b"hello")
    assert storage.read_file(upload_id) == b"hello"


@pytest.mark.anyio
async def test_read_file_async_matches_sync(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, 5, {})
    storage.write_chunk(upload_id, 0, b"hello")
    with patch.object(storage, "read_file", wraps=storage.read_file) as spy:
        data = await storage.read_file_async(upload_id)
    spy.assert_called_once_with(upload_id)
    assert data == b"hello"


@pytest.mark.anyio
async def test_set_upload_length_async_commits_deferred(storage):
    upload_id = _new_id()
    storage.create_upload(upload_id, None, {})
    with patch.object(
        storage, "set_upload_length", wraps=storage.set_upload_length
    ) as spy:
        await storage.set_upload_length_async(upload_id, 7)
    spy.assert_called_once_with(upload_id, 7)
    assert storage.get_upload(upload_id)["upload_length"] == 7


@pytest.mark.anyio
async def test_get_expired_uploads_async_returns_list(storage):
    expired_id = _new_id()
    storage.create_upload(
        expired_id,
        3,
        {},
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with patch.object(
        storage, "get_expired_uploads", wraps=storage.get_expired_uploads
    ) as spy:
        result = await storage.get_expired_uploads_async()
    spy.assert_called_once_with()
    assert expired_id in result


@pytest.mark.anyio
async def test_cleanup_expired_uploads_async_returns_count(storage):
    expired_id = _new_id()
    storage.create_upload(
        expired_id,
        3,
        {},
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with patch.object(
        storage,
        "cleanup_expired_uploads",
        wraps=storage.cleanup_expired_uploads,
    ) as spy:
        deleted = await storage.cleanup_expired_uploads_async()
    spy.assert_called_once_with()
    assert deleted >= 1
    assert storage.get_upload(expired_id) is None


@pytest.mark.anyio
async def test_concatenate_uploads_async_merges_partials(storage):
    p1 = _new_id()
    p2 = _new_id()
    for upload_id, data in ((p1, b"hello"), (p2, b"-world")):
        storage.create_upload(upload_id, len(data), {}, is_partial=True)
        storage.write_chunk(upload_id, 0, data)
        storage.update_offset(upload_id, len(data))
        storage.complete_upload(upload_id)

    final_id = _new_id()
    with patch.object(
        storage,
        "concatenate_uploads",
        wraps=storage.concatenate_uploads,
    ) as spy:
        total = await storage.concatenate_uploads_async(
            final_id, [p1, p2], {"name": "merged"}
        )
    spy.assert_called_once()
    assert total == len(b"hello-world")
    assert storage.read_file(final_id) == b"hello-world"
