"""Regression tests for concatenation-unfinished data-loss fixes:

- a partial referenced by a pending final must not be deletable (409);
- a crash between the assembly claim and completion must be recoverable.
"""

import os
import shutil
import sqlite3
import tempfile

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


def _h(**extra):
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


def _create_partial(server, length: int) -> str:
    _, headers, _ = server.handle_request(
        "POST",
        "/files",
        _h(**{"Upload-Length": str(length), "Upload-Concat": "partial"}),
        b"",
    )
    return headers["Location"]


def _patch(server, location: str, data: bytes, offset: int = 0) -> int:
    status, _, _ = server.handle_request(
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": str(offset),
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        data,
    )
    return status


class TestDeletePartialGuard:
    """A partial referenced by a pending final must not be deletable."""

    def test_delete_referenced_partial_409(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        a = _create_partial(server, 3)  # stays incomplete
        b = _create_partial(server, 3)
        assert _patch(server, b, b"bbb") == 204

        status, _, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Concat": f"final;{a} {b}"}), b""
        )
        assert status == 201

        # B is complete but still needed by the pending final.
        status, _, body = server.handle_request("DELETE", b, _h(), b"")
        assert status == 409
        assert b"unfinished concatenation" in body
        assert storage.get_upload(b.rsplit("/", 1)[1]) is not None

    def test_delete_allowed_after_assembly(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        a = _create_partial(server, 3)
        b = _create_partial(server, 3)
        assert _patch(server, b, b"bbb") == 204
        server.handle_request("POST", "/files", _h(**{"Upload-Concat": f"final;{a} {b}"}), b"")
        assert _patch(server, a, b"aaa") == 204  # completes → final assembles

        status, _, _ = server.handle_request("DELETE", b, _h(), b"")
        assert status == 204

    def test_delete_unreferenced_partial_allowed(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        a = _create_partial(server, 3)
        assert _patch(server, a, b"aaa") == 204
        status, _, _ = server.handle_request("DELETE", a, _h(), b"")
        assert status == 204

    @pytest.mark.anyio
    async def test_delete_referenced_partial_409_async(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        a = _create_partial(server, 3)
        b = _create_partial(server, 3)
        assert _patch(server, b, b"bbb") == 204
        server.handle_request("POST", "/files", _h(**{"Upload-Concat": f"final;{a} {b}"}), b"")

        status, _, _ = await server.handle_request_async("DELETE", b, _h(), b"")
        assert status == 409


class TestCrashSafeAssembly:
    def test_head_reclaims_a_crashed_assembly_claim(self, storage):
        """A crash between the claim UPDATE and complete_upload must be
        recoverable: the next HEAD re-claims and finishes assembly."""
        server = TusServer(storage=storage, base_path="/files")
        a = _create_partial(server, 3)
        b = _create_partial(server, 3)
        server.handle_request("POST", "/files", _h(**{"Upload-Concat": f"final;{a} {b}"}), b"")

        # Find the final's id (the only non-partial row).
        conn = sqlite3.connect(storage.db_path)
        (final_id,) = conn.execute("SELECT upload_id FROM uploads WHERE is_partial = 0").fetchone()

        # Complete both partials without triggering assembly (simulates the
        # window where assembly ran on another code path).
        orig = storage.find_pending_finals_for_partial
        storage.find_pending_finals_for_partial = lambda pid: []
        try:
            assert _patch(server, a, b"aaa") == 204
            assert _patch(server, b, b"bbb") == 204
        finally:
            storage.find_pending_finals_for_partial = orig

        # Simulate the crash: claim committed (upload_length = total), file
        # never copied, completed still 0.
        conn.execute("UPDATE uploads SET upload_length = 6 WHERE upload_id = ?", (final_id,))
        conn.commit()
        conn.close()

        status, headers, _ = server.handle_request("HEAD", f"/files/{final_id}", _h(), b"")
        assert status == 200
        assert headers["Upload-Offset"] == "6"
        assert headers["Upload-Length"] == "6"
        assert storage.read_file(final_id) == b"aaabbb"
