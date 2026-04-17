"""Tests for the Upload-Defer-Length extension."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def server():
    temp_dir = tempfile.mkdtemp()
    try:
        yield TusServer(
            storage=SQLiteStorage(
                db_path=os.path.join(temp_dir, "u.db"),
                upload_dir=os.path.join(temp_dir, "files"),
            ),
            base_path="/files",
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _h(**extra):
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


class TestDeferLengthServer:
    def test_options_advertises_creation_defer_length(self, server):
        status, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert status == 204
        exts = headers["Tus-Extension"].split(",")
        assert "creation-defer-length" in exts

    def test_create_with_defer_length(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b""
        )
        assert status == 201
        upload_id = headers["Location"].rsplit("/", 1)[1]
        stored = server.storage.get_upload(upload_id)
        assert stored is not None
        assert stored["upload_length"] is None

    def test_create_rejects_both_length_and_defer(self, server):
        status, _, body = server.handle_request(
            "POST",
            "/files",
            _h(**{"Upload-Length": "5", "Upload-Defer-Length": "1"}),
            b"",
        )
        assert status == 400
        assert b"mutually exclusive" in body or b"both" in body.lower()

    def test_create_rejects_missing_both(self, server):
        status, _, body = server.handle_request("POST", "/files", _h(), b"")
        assert status == 400
        assert b"Upload-Length" in body

    def test_head_reports_defer_length(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b""
        )
        location = headers["Location"]
        status, h, _ = server.handle_request("HEAD", location, _h(), b"")
        assert status == 200
        assert h.get("Upload-Defer-Length") == "1"
        assert "Upload-Length" not in h

    def test_first_patch_commits_length(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b""
        )
        location = headers["Location"]
        status, h, _ = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Length": "5",
                }
            ),
            b"hello",
        )
        assert status == 204
        assert h["Upload-Offset"] == "5"

        status, h, _ = server.handle_request("HEAD", location, _h(), b"")
        assert h["Upload-Length"] == "5"
        assert "Upload-Defer-Length" not in h

    def test_patch_without_length_on_deferred_upload_fails(self, server):
        """Deferred upload requires Upload-Length on the first PATCH."""
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b""
        )
        location = headers["Location"]
        status, _, body = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"hello",
        )
        assert status == 400
        assert b"Upload-Length" in body

    def test_cannot_change_length_after_commit(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b""
        )
        location = headers["Location"]
        # First PATCH commits length=10
        server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Length": "10",
                }
            ),
            b"hello",
        )
        # Second PATCH tries to change to 999 → reject
        status, _, _ = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "5",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Length": "999",
                }
            ),
            b"12345",
        )
        assert status == 400

    def test_resending_same_length_is_allowed(self, server):
        """Repeating the same Upload-Length on a subsequent PATCH is a no-op."""
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b""
        )
        location = headers["Location"]
        server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Length": "10",
                }
            ),
            b"hello",
        )
        status, _, _ = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "5",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Length": "10",  # same as before
                }
            ),
            b"world",
        )
        assert status == 204

    def test_client_create_deferred_upload(self, server):
        """TusClient.create_deferred_upload creates a deferred upload server-side."""
        import threading
        from http.server import HTTPServer

        from resumable_upload.client import TusClient
        from resumable_upload.server import TusHTTPRequestHandler

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = server
        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            client = TusClient(f"http://127.0.0.1:{port}/files")
            url = client.create_deferred_upload(metadata={"filename": "stream.bin"})
            upload_id = url.rsplit("/", 1)[1]
            stored = server.storage.get_upload(upload_id)
            assert stored is not None
            assert stored["upload_length"] is None
            assert stored["metadata"] == {"filename": "stream.bin"}
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_completion_after_deferred_length(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b""
        )
        location = headers["Location"]
        upload_id = location.rsplit("/", 1)[1]
        server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Length": "5",
                }
            ),
            b"hello",
        )
        stored = server.storage.get_upload(upload_id)
        assert stored["completed"] is True
