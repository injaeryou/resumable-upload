"""Tests for the expanded hook surface (tusd parity):

- on_chunk_received  — fires after every accepted PATCH chunk (post-receive);
  raising TusHookError stops and deletes the upload (StopUpload).
- on_upload_complete return value — dict merged into the finishing request's
  response (pre-finish custom response).
- on_before_terminate — blocking pre-hook that can veto DELETE (pre-terminate).
- TusServer.terminate_upload() — out-of-band server-initiated termination.
"""

import os
import shutil
import tempfile

import pytest

from resumable_upload.exceptions import TusHookError
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


def _create(server, length: int) -> str:
    status, headers, _ = server.handle_request(
        "POST", "/files", _h(**{"Upload-Length": str(length)}), b""
    )
    assert status == 201
    return headers["Location"]


def _patch(server, location, offset, data):
    return server.handle_request(
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


class TestOnChunkReceived:
    def test_fires_per_chunk_with_args(self, storage):
        calls = []
        server = TusServer(
            storage=storage,
            on_chunk_received=lambda uid, offset, length: calls.append((uid, offset, length)),
        )
        loc = _create(server, 10)
        uid = loc.rsplit("/", 1)[1]

        assert _patch(server, loc, 0, b"12345")[0] == 204
        assert _patch(server, loc, 5, b"67890")[0] == 204
        assert calls == [(uid, 5, 5), (uid, 10, 5)]

    def test_hook_error_stops_and_deletes_upload(self, storage):
        # StopUpload semantics: quota exceeded mid-transfer → 429 + upload gone.
        def hook(uid, offset, length):
            if offset >= 5:
                raise TusHookError("quota exceeded", status_code=429)

        server = TusServer(storage=storage, on_chunk_received=hook)
        loc = _create(server, 10)

        status, _, body = _patch(server, loc, 0, b"12345")
        assert status == 429
        assert b"quota" in body

        status, _, _ = server.handle_request("HEAD", loc, _h(), b"")
        assert status == 404, "stopped upload must be deleted"

    def test_plain_exception_swallowed(self, storage):
        def hook(uid, offset, length):
            raise ValueError("boom")

        server = TusServer(storage=storage, on_chunk_received=hook)
        loc = _create(server, 5)
        status, _, _ = _patch(server, loc, 0, b"hello")
        assert status == 204

    @pytest.mark.anyio
    async def test_async_fires_and_stops(self, storage):
        calls = []

        def hook(uid, offset, length):
            calls.append(offset)
            if offset >= 10:
                raise TusHookError("too much", status_code=413)

        server = TusServer(storage=storage, on_chunk_received=hook)
        loc = _create(server, 10)

        status, _, _ = await server.handle_request_async(
            "PATCH",
            loc,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"1234567890",
        )
        assert status == 413
        assert calls == [10]
        status, _, _ = await server.handle_request_async("HEAD", loc, _h(), b"")
        assert status == 404


class TestCompletionResponse:
    def test_dict_return_merges_into_final_patch(self, storage):
        def on_complete(uid, metadata, file_info):
            return {
                "status_code": 200,
                "headers": {"X-Result-URL": f"https://cdn.example/{uid}"},
                "body": "processed",
            }

        server = TusServer(storage=storage, on_upload_complete=on_complete)
        loc = _create(server, 5)
        uid = loc.rsplit("/", 1)[1]

        status, headers, body = _patch(server, loc, 0, b"hello")
        assert status == 200
        assert headers["X-Result-URL"] == f"https://cdn.example/{uid}"
        assert body == b"processed"
        # Standard TUS headers must survive the merge.
        assert headers["Upload-Offset"] == "5"

    def test_intermediate_patch_untouched(self, storage):
        server = TusServer(
            storage=storage,
            on_upload_complete=lambda *a: {"status_code": 200, "body": "done"},
        )
        loc = _create(server, 10)
        status, _, body = _patch(server, loc, 0, b"12345")
        assert status == 204
        assert body == b""

    def test_none_return_keeps_204(self, storage):
        server = TusServer(storage=storage, on_upload_complete=lambda *a: None)
        loc = _create(server, 5)
        status, _, _ = _patch(server, loc, 0, b"hello")
        assert status == 204

    def test_merges_into_creation_with_upload_response(self, storage):
        server = TusServer(
            storage=storage,
            on_upload_complete=lambda *a: {"headers": {"X-Done": "1"}},
        )
        data = b"oneshot"
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            _h(
                **{
                    "Upload-Length": str(len(data)),
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            data,
        )
        assert status == 201
        assert headers["X-Done"] == "1"

    @pytest.mark.anyio
    async def test_async_final_patch_merge(self, storage):
        server = TusServer(
            storage=storage,
            on_upload_complete=lambda *a: {"status_code": 200, "body": "ok"},
        )
        loc = _create(server, 5)
        status, _, body = await server.handle_request_async(
            "PATCH",
            loc,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"hello",
        )
        assert status == 200
        assert body == b"ok"


class TestBeforeTerminate:
    def test_veto_blocks_delete(self, storage):
        def veto(uid):
            raise TusHookError("termination disabled", status_code=403)

        server = TusServer(storage=storage, on_before_terminate=veto)
        loc = _create(server, 5)

        status, _, body = server.handle_request("DELETE", loc, _h(), b"")
        assert status == 403
        assert b"termination disabled" in body
        # Upload untouched.
        status, _, _ = server.handle_request("HEAD", loc, _h(), b"")
        assert status == 200

    def test_allow_passes_through(self, storage):
        server = TusServer(storage=storage, on_before_terminate=lambda uid: None)
        loc = _create(server, 5)
        status, _, _ = server.handle_request("DELETE", loc, _h(), b"")
        assert status == 204

    @pytest.mark.anyio
    async def test_async_veto(self, storage):
        def veto(uid):
            raise TusHookError("no", status_code=403)

        server = TusServer(storage=storage, on_before_terminate=veto)
        loc = _create(server, 5)
        status, _, _ = await server.handle_request_async("DELETE", loc, _h(), b"")
        assert status == 403


class TestServerInitiatedTerminate:
    def test_terminate_upload_deletes_and_fires_hook(self, storage):
        terminated = []
        server = TusServer(storage=storage, on_upload_terminate=terminated.append)
        loc = _create(server, 5)
        uid = loc.rsplit("/", 1)[1]

        assert server.terminate_upload(uid) is True
        assert terminated == [uid]
        status, _, _ = server.handle_request("HEAD", loc, _h(), b"")
        assert status == 404

    def test_terminate_unknown_returns_false(self, storage):
        server = TusServer(storage=storage)
        assert server.terminate_upload("00000000-0000-0000-0000-000000000000") is False
