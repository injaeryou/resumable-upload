"""Tests for the (non-standard, opt-in) GET download endpoint.

tusd serves completed uploads over GET by default; this library is opt-in
(``TusServer(enable_downloads=True)``) because it is usually embedded in a
framework that may already route GET on the same path.
"""

from __future__ import annotations

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


def _upload(server, data: bytes, metadata: str | None = None) -> str:
    """Create + fully upload; return the upload path."""
    headers = {"Upload-Length": str(len(data))}
    if metadata:
        headers["Upload-Metadata"] = metadata
    status, post_headers, _ = server.handle_request("POST", "/files", _h(**headers), b"")
    assert status == 201
    location = post_headers["Location"]
    status, _, _ = server.handle_request(
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        data,
    )
    assert status == 204
    return location


def _b64(s: str) -> str:
    import base64

    return base64.b64encode(s.encode()).decode()


def _body_bytes(body) -> bytes:
    """Download bodies are streams (never whole files in RAM); drain them."""
    if isinstance(body, bytes):
        return body
    try:
        return body.read()
    finally:
        body.close()


class TestDownloadDisabled:
    def test_get_returns_404_by_default(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        location = _upload(server, b"hello")
        status, _, _ = server.handle_request("GET", location, _h(), b"")
        assert status == 404


class TestDownloadEnabled:
    @pytest.fixture
    def server(self, storage):
        return TusServer(storage=storage, base_path="/files", enable_downloads=True)

    def test_completed_upload_served(self, server):
        location = _upload(server, b"hello world")
        status, headers, body = server.handle_request("GET", location, _h(), b"")
        assert status == 200
        assert not isinstance(body, bytes)  # must stream, not buffer
        assert _body_bytes(body) == b"hello world"
        assert headers["Content-Length"] == "11"
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["Content-Disposition"].startswith("attachment")

    def test_content_type_from_metadata_filetype(self, server):
        location = _upload(server, b"fake-png", metadata=f"filetype {_b64('image/png')}")
        status, headers, _ = server.handle_request("GET", location, _h(), b"")
        assert status == 200
        assert headers["Content-Type"] == "image/png"

    def test_invalid_filetype_falls_back_to_octet_stream(self, server):
        location = _upload(server, b"x", metadata=f"filetype {_b64('not a mime !!')}")
        status, headers, _ = server.handle_request("GET", location, _h(), b"")
        assert status == 200
        assert headers["Content-Type"] == "application/octet-stream"

    def test_html_filetype_served_as_attachment(self, server):
        # Anti-XSS: even a valid text/html filetype must never render inline.
        location = _upload(server, b"<script>", metadata=f"filetype {_b64('text/html')}")
        status, headers, _ = server.handle_request("GET", location, _h(), b"")
        assert status == 200
        assert headers["Content-Disposition"].startswith("attachment")

    def test_filename_sanitized_in_disposition(self, server):
        evil = 'a"b\r\nSet-Cookie: x=1;.txt'
        location = _upload(server, b"x", metadata=f"filename {_b64(evil)}")
        status, headers, _ = server.handle_request("GET", location, _h(), b"")
        assert status == 200
        disposition = headers["Content-Disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        # The quoted filename must not contain raw quotes or CR/LF.
        filename = disposition.split('filename="', 1)[1].rsplit('"', 1)[0]
        assert '"' not in filename
        assert "Set-Cookie" not in headers

    def test_incomplete_upload_not_served(self, server):
        status, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Length": "10"}), b""
        )
        location = headers["Location"]
        server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"abc",
        )
        status, _, body = server.handle_request("GET", location, _h(), b"")
        assert status == 404
        assert b"abc" not in body

    def test_unknown_upload_404(self, server):
        status, _, _ = server.handle_request(
            "GET", "/files/00000000-0000-0000-0000-000000000000", _h(), b""
        )
        assert status == 404

    def test_expired_upload_410(self, storage):
        from datetime import datetime, timedelta, timezone

        server = TusServer(
            storage=storage,
            base_path="/files",
            enable_downloads=True,
            cleanup_interval=10**6,  # keep the expired row so GET sees it
        )
        uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        storage.create_upload(uid, 5, {}, expires_at=past)
        storage.write_chunk(uid, 0, b"hello")
        storage.update_offset(uid, 5)
        storage.complete_upload(uid)

        status, _, _ = server.handle_request("GET", f"/files/{uid}", _h(), b"")
        assert status == 410

    def test_invalid_upload_id_400(self, server):
        status, _, _ = server.handle_request("GET", "/files/../etc/passwd", _h(), b"")
        assert status in (400, 404)

    @pytest.mark.anyio
    async def test_async_dispatch_serves_download(self, server):
        location = _upload(server, b"hello")
        status, headers, body = await server.handle_request_async("GET", location, _h(), b"")
        assert status == 200
        assert _body_bytes(body) == b"hello"


class TestDownloadTransports:
    def test_http_handler_serves_download_and_metrics(self, storage):
        import threading
        import urllib.request
        from http.server import HTTPServer

        from resumable_upload.metrics import MetricsRegistry
        from resumable_upload.server import TusHTTPRequestHandler

        server = TusServer(
            storage=storage,
            base_path="/files",
            enable_downloads=True,
            metrics_registry=MetricsRegistry(),
        )
        location = _upload(server, b"hello")

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = server
        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{location}") as resp:
                assert resp.status == 200
                assert resp.read() == b"hello"
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as resp:
                assert resp.status == 200
        finally:
            httpd.shutdown()
            httpd.server_close()

    @pytest.mark.anyio
    async def test_asgi_serves_download(self, storage):
        from resumable_upload.asgi import TusASGIApp

        server = TusServer(storage=storage, base_path="/files", enable_downloads=True)
        location = _upload(server, b"hello")
        app = TusASGIApp(server)

        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": location, "headers": []}
        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert start["status"] == 200
        assert body == b"hello"
