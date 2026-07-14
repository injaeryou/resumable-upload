"""Regression: an on_upload_complete body without a status_code must not ship
as an invalid 204-with-body; it is promoted to 200.
"""

import os
import shutil
import tempfile

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage


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


def _complete(server, headers_loc):
    return server.handle_request(
        "PATCH",
        headers_loc,
        _h(**{"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"}),
        b"hi",
    )


class TestCompletionHookResponse:
    def test_body_without_status_promotes_204_to_200(self, storage):
        server = TusServer(
            storage=storage,
            base_path="/files",
            on_upload_complete=lambda *a: {"body": '{"ok": true}'},
        )
        _, headers, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "2"}), b"")
        status, resp_headers, body = _complete(server, headers["Location"])
        assert status == 200  # not an invalid 204-with-body
        assert body == b'{"ok": true}'
        assert resp_headers["Content-Length"] == str(len(body))

    def test_explicit_status_code_respected(self, storage):
        server = TusServer(
            storage=storage,
            base_path="/files",
            on_upload_complete=lambda *a: {"status_code": 201, "body": "made"},
        )
        _, headers, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "2"}), b"")
        status, _, body = _complete(server, headers["Location"])
        assert status == 201
        assert body == b"made"

    def test_headers_only_result_keeps_204(self, storage):
        server = TusServer(
            storage=storage,
            base_path="/files",
            on_upload_complete=lambda *a: {"headers": {"X-Post-Process": "queued"}},
        )
        _, headers, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "2"}), b"")
        status, resp_headers, body = _complete(server, headers["Location"])
        assert status == 204
        assert body == b""
        assert resp_headers["X-Post-Process"] == "queued"
