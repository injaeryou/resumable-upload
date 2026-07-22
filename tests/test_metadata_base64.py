"""Regression: ``Upload-Metadata`` base64 decoding silently discarded
non-alphabet characters instead of rejecting the value.
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


class TestMetadataBase64Validation:
    def test_non_alphabet_base64_rejected(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        status, _, body = server.handle_request(
            "POST", "/files", _h(**{"Upload-Length": "1", "Upload-Metadata": "filename ####"})
        )
        assert status == 400
        assert b"Invalid base64" in body

    def test_valid_base64_still_accepted(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _h(**{"Upload-Length": "1", "Upload-Metadata": "filename dGVzdC5iaW4="}),
        )
        assert status == 201
