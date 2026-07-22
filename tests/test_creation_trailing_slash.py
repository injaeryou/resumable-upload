"""Regression: the creation endpoint only matched its path exactly, 404ing a
POST with a trailing slash — which tusd and tus-py-client both produce.
"""

import asyncio
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


class TestCreationTrailingSlash:
    def test_post_with_trailing_slash_creates(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        status, headers, _ = server.handle_request("POST", "/files/", _h(**{"Upload-Length": "3"}))
        assert status == 201
        assert headers["Location"].startswith("/files/")

    def test_post_without_trailing_slash_still_creates(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        status, _, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "3"}))
        assert status == 201

    def test_post_trailing_slash_async(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        status, _, _ = asyncio.run(
            server.handle_request_async("POST", "/files/", _h(**{"Upload-Length": "3"}))
        )
        assert status == 201
