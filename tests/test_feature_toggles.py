"""tusd-style feature toggles: disable_termination / disable_concatenation."""

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


def _extensions(server):
    _, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
    return headers["Tus-Extension"].split(",")


class TestDisableTermination:
    @pytest.fixture
    def server(self, storage):
        return TusServer(storage=storage, disable_termination=True)

    def test_not_advertised(self, server):
        assert "termination" not in _extensions(server)

    def test_delete_rejected_405(self, server):
        _, headers, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "5"}), b"")
        status, _, _ = server.handle_request("DELETE", headers["Location"], _h(), b"")
        assert status == 405
        # Upload untouched.
        status, _, _ = server.handle_request("HEAD", headers["Location"], _h(), b"")
        assert status == 200

    def test_server_side_terminate_still_works(self, server):
        # The toggle guards *client* DELETEs only.
        _, headers, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "5"}), b"")
        uid = headers["Location"].rsplit("/", 1)[1]
        assert server.terminate_upload(uid) is True


class TestDisableConcatenation:
    @pytest.fixture
    def server(self, storage):
        return TusServer(storage=storage, disable_concatenation=True)

    def test_not_advertised(self, server):
        exts = _extensions(server)
        assert "concatenation" not in exts
        assert "concatenation-unfinished" not in exts

    def test_partial_creation_rejected(self, server):
        status, _, body = server.handle_request(
            "POST",
            "/files",
            _h(**{"Upload-Length": "5", "Upload-Concat": "partial"}),
            b"",
        )
        assert status == 400
        assert b"concatenation" in body.lower()

    def test_final_creation_rejected(self, server):
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _h(**{"Upload-Concat": "final;/files/00000000-0000-0000-0000-000000000000"}),
            b"",
        )
        assert status == 400

    def test_plain_upload_still_works(self, server):
        status, _, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "5"}), b"")
        assert status == 201


class TestDefaultsUnchanged:
    def test_both_enabled_by_default(self, storage):
        server = TusServer(storage=storage)
        exts = _extensions(server)
        assert "termination" in exts
        assert "concatenation" in exts
