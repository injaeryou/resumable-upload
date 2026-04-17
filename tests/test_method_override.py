"""Tests for X-HTTP-Method-Override support."""

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


class TestMethodOverride:
    def test_post_override_to_patch(self, server):
        # Create an upload first.
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Length": "5"}), b""
        )
        assert status == 201
        location = headers["Location"]

        # Now PATCH via POST + X-HTTP-Method-Override.
        status, h, _ = server.handle_request(
            "POST",
            location,
            _h(
                **{
                    "X-HTTP-Method-Override": "PATCH",
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"hello",
        )
        assert status == 204
        assert h["Upload-Offset"] == "5"

    def test_post_override_to_delete(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Length": "5"}), b""
        )
        assert status == 201
        location = headers["Location"]

        status, _, _ = server.handle_request(
            "POST",
            location,
            _h(**{"X-HTTP-Method-Override": "DELETE"}),
            b"",
        )
        assert status == 204
        # The upload should be gone.
        upload_id = location.rsplit("/", 1)[1]
        assert server.storage.get_upload(upload_id) is None

    def test_post_override_to_head(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Length": "10"}), b""
        )
        location = headers["Location"]

        status, h, _ = server.handle_request(
            "POST",
            location,
            _h(**{"X-HTTP-Method-Override": "HEAD"}),
            b"",
        )
        assert status == 200
        assert h["Upload-Length"] == "10"

    def test_override_is_case_insensitive(self, server):
        """Override header value and header name should both be case-insensitive."""
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Length": "5"}), b""
        )
        location = headers["Location"]

        status, _, _ = server.handle_request(
            "POST",
            location,
            _h(**{"x-http-method-override": "delete"}),
            b"",
        )
        assert status == 204

    def test_override_to_options_rejected(self, server):
        # Override to OPTIONS would sidestep the Tus-Resumable check; reject.
        status, _, body = server.handle_request(
            "POST",
            "/files",
            _h(**{"X-HTTP-Method-Override": "OPTIONS"}),
            b"",
        )
        assert status == 400
        assert b"X-HTTP-Method-Override" in body or b"OPTIONS" in body

    def test_override_to_get_rejected(self, server):
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _h(**{"X-HTTP-Method-Override": "GET"}),
            b"",
        )
        assert status == 400

    def test_override_unknown_method_rejected(self, server):
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _h(**{"X-HTTP-Method-Override": "FROB"}),
            b"",
        )
        assert status == 400

    def test_override_ignored_on_non_post(self, server):
        """Override header on a real PATCH must NOT rewrite — TUS spec only allows POST."""
        # No upload exists; a genuine PATCH should 404 regardless of override header
        status, _, _ = server.handle_request(
            "PATCH",
            "/files/00000000-0000-0000-0000-000000000000",
            _h(
                **{
                    "X-HTTP-Method-Override": "DELETE",
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"",
        )
        assert status == 404  # would be 204 if override had been honored

    def test_empty_override_ignored(self, server):
        """Empty override header falls through to normal POST handling."""
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _h(**{"Upload-Length": "5", "X-HTTP-Method-Override": ""}),
            b"",
        )
        # Empty override is a no-op — POST to /files with Upload-Length creates
        assert status == 201
