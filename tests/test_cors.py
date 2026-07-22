"""Tests for CORS configuration depth: origin lists, credentials, max-age."""

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


def _h(**extra):
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


class TestBackCompatString:
    def test_static_string_behaves_as_before(self, storage):
        server = TusServer(storage=storage, cors_allow_origins="*")
        status, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert "Access-Control-Expose-Headers" in headers
        assert "Access-Control-Allow-Credentials" not in headers
        assert "Access-Control-Max-Age" not in headers

    def test_no_cors_config_no_headers(self, storage):
        server = TusServer(storage=storage)
        _, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert "Access-Control-Allow-Origin" not in headers


class TestOriginList:
    @pytest.fixture
    def server(self, storage):
        return TusServer(
            storage=storage,
            cors_allow_origins=["https://app.example", "https://admin.example"],
        )

    def test_matching_origin_echoed_with_vary(self, server):
        _, headers, _ = server.handle_request(
            "OPTIONS", "/files", {"Origin": "https://app.example"}, b""
        )
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"
        assert headers["Vary"] == "Origin"

    def test_non_matching_origin_gets_no_cors(self, server):
        _, headers, _ = server.handle_request(
            "OPTIONS", "/files", {"Origin": "https://evil.example"}, b""
        )
        assert "Access-Control-Allow-Origin" not in headers

    def test_missing_origin_gets_no_cors(self, server):
        _, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert "Access-Control-Allow-Origin" not in headers

    def test_error_responses_carry_cors(self, server):
        status, headers, _ = server.handle_request(
            "HEAD",
            "/files/00000000-0000-0000-0000-000000000000",
            _h(Origin="https://app.example"),
            b"",
        )
        assert status == 404
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"

    @pytest.mark.anyio
    async def test_async_matching_origin(self, server):
        _, headers, _ = await server.handle_request_async(
            "OPTIONS", "/files", {"Origin": "https://app.example"}, b""
        )
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"


class TestCredentials:
    def test_credentials_with_specific_origin(self, storage):
        server = TusServer(
            storage=storage,
            cors_allow_origins=["https://app.example"],
            cors_allow_credentials=True,
        )
        _, headers, _ = server.handle_request(
            "OPTIONS", "/files", {"Origin": "https://app.example"}, b""
        )
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"
        assert headers["Access-Control-Allow-Credentials"] == "true"

    def test_credentials_with_wildcard_echoes_origin(self, storage):
        # `Access-Control-Allow-Origin: *` is invalid with credentials —
        # the request origin must be echoed instead.
        server = TusServer(storage=storage, cors_allow_origins="*", cors_allow_credentials=True)
        _, headers, _ = server.handle_request(
            "OPTIONS", "/files", {"Origin": "https://app.example"}, b""
        )
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"
        assert headers["Access-Control-Allow-Credentials"] == "true"
        assert headers["Vary"] == "Origin"


class TestMaxAge:
    def test_max_age_on_preflight_only(self, storage):
        server = TusServer(storage=storage, cors_allow_origins="*", cors_max_age=86400)
        _, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert headers["Access-Control-Max-Age"] == "86400"

        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Length": "5"}), b""
        )
        assert status == 201
        assert "Access-Control-Max-Age" not in headers
