"""Tests for the TUS concatenation-unfinished extension.

A client may POST ``Upload-Concat: final;<urls>`` while the referenced
partial uploads are still in progress. The final upload stays *pending*
(no Upload-Offset / Upload-Length) until the last partial completes, at
which point the server assembles the bytes and fires on_upload_complete.
"""

import os
import shutil
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


@pytest.fixture
def server(storage):
    return TusServer(storage=storage, base_path="/files")


def _h(**extra):
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


def _create_partial(server, length: int) -> str:
    status, headers, _ = server.handle_request(
        "POST",
        "/files",
        _h(**{"Upload-Length": str(length), "Upload-Concat": "partial"}),
        b"",
    )
    assert status == 201
    return headers["Location"]


def _patch(server, location: str, offset: int, data: bytes) -> int:
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


def _post_final(server, locations: list[str]):
    return server.handle_request(
        "POST",
        "/files",
        _h(**{"Upload-Concat": "final;" + " ".join(locations)}),
        b"",
    )


class TestUnfinishedCreate:
    def test_post_final_over_incomplete_partial_returns_pending_201(self, server):
        p = _create_partial(server, 5)  # no data uploaded yet

        status, headers, _ = _post_final(server, [p])
        assert status == 201
        assert "Location" in headers
        # Length/offset unknown until the partials finish.
        assert "Upload-Offset" not in headers
        assert "Upload-Length" not in headers

    def test_head_pending_final_echoes_concat_without_length(self, server):
        p1 = _create_partial(server, 5)
        p2 = _create_partial(server, 6)
        _patch(server, p1, 0, b"hello")  # p1 complete, p2 still empty

        _, headers, _ = _post_final(server, [p1, p2])
        final_loc = headers["Location"]

        status, head_headers, _ = server.handle_request("HEAD", final_loc, _h(), b"")
        assert status == 200
        assert head_headers["Upload-Concat"] == f"final;{p1} {p2}"
        assert "Upload-Length" not in head_headers
        assert head_headers["Upload-Offset"] == "0"

    def test_patch_rejected_on_pending_final(self, server):
        p = _create_partial(server, 5)
        _, headers, _ = _post_final(server, [p])
        status = _patch(server, headers["Location"], 0, b"x")
        assert status == 403

    def test_post_final_rejects_when_total_exceeds_max_size(self, storage):
        server = TusServer(storage=storage, base_path="/files", max_size=8)
        p1 = _create_partial(server, 5)
        p2 = _create_partial(server, 6)  # 5 + 6 > 8

        status, headers, _ = _post_final(server, [p1, p2])
        assert status == 413
        assert "Location" not in headers

    def test_post_final_still_rejects_missing_partial(self, server):
        status, _, _ = _post_final(server, ["/files/00000000-0000-0000-0000-000000000000"])
        assert status == 400


class TestAssemblyOnLastPartial:
    def test_completing_last_partial_assembles_final(self, server):
        p1 = _create_partial(server, 5)
        p2 = _create_partial(server, 6)
        _patch(server, p1, 0, b"hello")

        _, headers, _ = _post_final(server, [p1, p2])
        final_loc = headers["Location"]
        final_id = final_loc.rsplit("/", 1)[1]

        assert _patch(server, p2, 0, b"-world") == 204

        status, head_headers, _ = server.handle_request("HEAD", final_loc, _h(), b"")
        assert status == 200
        assert head_headers["Upload-Offset"] == "11"
        assert head_headers["Upload-Length"] == "11"
        assert server.storage.read_file(final_id) == b"hello-world"
        assert server.storage.get_upload(final_id)["completed"] is True

    def test_two_pending_finals_sharing_a_partial_both_assemble(self, server):
        shared = _create_partial(server, 2)
        solo1 = _create_partial(server, 2)
        solo2 = _create_partial(server, 2)
        _patch(server, solo1, 0, b"AA")
        _patch(server, solo2, 0, b"BB")

        _, h1, _ = _post_final(server, [solo1, shared])
        _, h2, _ = _post_final(server, [shared, solo2])

        _patch(server, shared, 0, b"XX")

        f1 = h1["Location"].rsplit("/", 1)[1]
        f2 = h2["Location"].rsplit("/", 1)[1]
        assert server.storage.read_file(f1) == b"AAXX"
        assert server.storage.read_file(f2) == b"XXBB"

    def test_on_upload_complete_fires_once_at_assembly(self, storage):
        calls = []

        def on_complete(upload_id, metadata, file_info):
            calls.append(upload_id)

        server = TusServer(storage=storage, base_path="/files", on_upload_complete=on_complete)
        p = _create_partial(server, 5)
        _, headers, _ = _post_final(server, [p])
        final_id = headers["Location"].rsplit("/", 1)[1]

        assert calls == []  # nothing completed yet (partials never fire it)

        _patch(server, p, 0, b"hello")
        assert calls == [final_id]

    def test_delete_pending_final_leaves_partials(self, server):
        p = _create_partial(server, 5)
        _, headers, _ = _post_final(server, [p])
        final_loc = headers["Location"]

        status, _, _ = server.handle_request("DELETE", final_loc, _h(), b"")
        assert status == 204

        # Partial untouched and still completable.
        assert _patch(server, p, 0, b"hello") == 204
        # Deleted pending final never assembles.
        status, _, _ = server.handle_request("HEAD", final_loc, _h(), b"")
        assert status == 404


class TestAdvertisement:
    def test_options_advertises_when_storage_supports(self, server):
        _, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert "concatenation-unfinished" in headers["Tus-Extension"].split(",")

    def test_options_silent_when_storage_lacks_support(self, storage):
        class NoUnfinished(SQLiteStorage):
            supports_unfinished_concat = False

        temp_dir = tempfile.mkdtemp()
        try:
            server = TusServer(
                storage=NoUnfinished(
                    db_path=os.path.join(temp_dir, "u.db"),
                    upload_dir=os.path.join(temp_dir, "files"),
                ),
                base_path="/files",
            )
            _, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
            assert "concatenation-unfinished" not in headers["Tus-Extension"].split(",")

            # Without support, the old strict behavior applies: 400 on
            # incomplete partials.
            p = _create_partial(server, 5)
            status, _, _ = _post_final(server, [p])
            assert status == 400
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestUnfinishedAsync:
    @pytest.mark.anyio
    async def test_async_pending_create_head_and_assembly(self, server):
        p = _create_partial(server, 5)

        status, headers, _ = await server.handle_request_async(
            "POST",
            "/files",
            _h(**{"Upload-Concat": f"final;{p}"}),
            b"",
        )
        assert status == 201
        assert "Upload-Offset" not in headers
        final_loc = headers["Location"]

        status, head_headers, _ = await server.handle_request_async("HEAD", final_loc, _h(), b"")
        assert status == 200
        assert head_headers["Upload-Concat"] == f"final;{p}"
        assert "Upload-Length" not in head_headers

        status, _, _ = await server.handle_request_async(
            "PATCH",
            p,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"hello",
        )
        assert status == 204

        status, head_headers, _ = await server.handle_request_async("HEAD", final_loc, _h(), b"")
        assert head_headers["Upload-Offset"] == "5"
        assert head_headers["Upload-Length"] == "5"
