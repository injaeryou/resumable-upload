"""Regression: the CORS allow-headers list must include the override-tunnel
headers, else a browser preflight for PATCH-over-POST is rejected.
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


class TestCorsAllowHeaders:
    def test_preflight_allows_override_tunnel_headers(self, storage):
        server = TusServer(
            storage=storage, base_path="/files", cors_allow_origins="https://app.example"
        )
        _, headers, _ = server.handle_request(
            "OPTIONS", "/files", {"Origin": "https://app.example"}, b""
        )
        allow = headers["Access-Control-Allow-Headers"]
        assert "X-HTTP-Method-Override" in allow
        assert "X-Request-ID" in allow
        assert "Upload-Defer-Length" in allow

    def test_cors_headers_on_413(self, storage):
        server = TusServer(
            storage=storage,
            base_path="/files",
            max_size=4,
            cors_allow_origins="https://app.example",
        )
        _, headers, _ = server.handle_request(
            "POST",
            "/files",
            {
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "100",
                "Content-Type": "application/offset+octet-stream",
                "Origin": "https://app.example",
            },
            b"toolong",
        )
        assert headers.get("Access-Control-Allow-Origin") == "https://app.example"
