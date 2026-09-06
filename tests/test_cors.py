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


class TestTransportRejectionsCarryCORS:
    """The handler's own 400/413 gates short-circuit before the core.

    tus-js-client and uppy send Content-Length, not chunked, so these are the
    rejections a browser actually hits — without CORS the response is opaque
    cross-origin and the client reports a generic network error, not the status.
    """

    @pytest.fixture
    def port(self, storage):
        import threading
        from http.server import HTTPServer

        from resumable_upload.server import TusHTTPRequestHandler

        tus = TusServer(
            storage=storage,
            base_path="/files",
            cors_allow_origins="https://app.example",
            max_chunk_size=1024,
            max_size=8192,
        )

        class Handler(TusHTTPRequestHandler):
            tus_server = tus

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()

    def _patch(self, port, body, content_length):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/files/nonexistent",
            data=body,
            method="PATCH",
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
                "Content-Length": str(content_length),
                "Origin": "https://app.example",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers

    def test_chunk_over_cap_is_readable_cross_origin(self, port):
        status, headers = self._patch(port, b"x" * 2048, 2048)
        assert status == 413
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"

    def test_body_over_max_size_is_readable_cross_origin(self, port):
        status, headers = self._patch(port, b"x" * 9000, 9000)
        assert status == 413
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"

    def test_malformed_content_length_is_readable_cross_origin(self, port):
        status, headers = self._patch(port, b"", "not-a-number")
        assert status == 400
        assert headers["Access-Control-Allow-Origin"] == "https://app.example"
