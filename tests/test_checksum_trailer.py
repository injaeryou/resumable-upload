"""Tests for the TUS checksum-trailer extension.

Trailers only exist on chunked transfer-encoding requests, which the
stdlib transport (``TusHTTPRequestHandler``) must parse itself — so these
tests speak raw HTTP over a socket against a live server.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import shutil
import socket
import tempfile
import threading
import urllib.request
from http.server import HTTPServer

import pytest

from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def live_server():
    """In-process TUS server with checksum-trailer enabled; yields (port, storage)."""
    temp_dir = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(temp_dir, "u.db"),
        upload_dir=os.path.join(temp_dir, "files"),
    )
    tus = TusServer(
        storage=storage,
        base_path="/files",
        supports_checksum_trailer=True,
        max_chunk_size=1024,
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, storage
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _create_upload(port: int, length: int) -> str:
    """POST a new upload via urllib; return its upload id."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/files",
        method="POST",
        headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(length)},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Location"].rsplit("/", 1)[1]


def _raw(port: int, payload: bytes) -> tuple[int, dict[str, str]]:
    """Send raw HTTP bytes; return (status, headers)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        sock.settimeout(5)
        data = b""
        while True:
            got = sock.recv(65536)
            if not got:
                break
            data += got
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return status, headers


def _chunked_patch(
    port: int,
    upload_id: str,
    data: bytes,
    trailer: str | None,
    *,
    chunk_size_line: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str]]:
    headers = {
        "Host": "127.0.0.1",
        "Tus-Resumable": "1.0.0",
        "Upload-Offset": "0",
        "Content-Type": "application/offset+octet-stream",
        "Transfer-Encoding": "chunked",
        "Connection": "close",
    }
    if trailer:
        headers["Trailer"] = "Upload-Checksum"
    if extra_headers:
        headers.update(extra_headers)

    head = f"PATCH /files/{upload_id} HTTP/1.1\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    head += "\r\n"

    size_line = chunk_size_line if chunk_size_line is not None else f"{len(data):x}"
    payload = head.encode("latin-1")
    payload += f"{size_line}\r\n".encode("latin-1") + data + b"\r\n"
    payload += b"0\r\n"
    if trailer:
        payload += f"Upload-Checksum: {trailer}\r\n".encode("latin-1")
    payload += b"\r\n"
    return _raw(port, payload)


def _sha1_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")


def _offset(port: int, upload_id: str) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/files/{upload_id}",
        method="HEAD",
        headers={"Tus-Resumable": "1.0.0"},
    )
    with urllib.request.urlopen(req) as resp:
        return int(resp.headers["Upload-Offset"])


class TestChunkedTrailer:
    def test_valid_sha1_trailer_accepted(self, live_server):
        port, _ = live_server
        uid = _create_upload(port, 5)
        status, _ = _chunked_patch(port, uid, b"hello", f"sha1 {_sha1_b64(b'hello')}")
        assert status == 204
        assert _offset(port, uid) == 5

    def test_wrong_trailer_checksum_rejected_460(self, live_server):
        port, _ = live_server
        uid = _create_upload(port, 5)
        status, _ = _chunked_patch(port, uid, b"hello", f"sha1 {_sha1_b64(b'other')}")
        assert status == 460
        assert _offset(port, uid) == 0

    def test_unsupported_trailer_algorithm_rejected_400(self, live_server):
        port, _ = live_server
        uid = _create_upload(port, 5)
        status, _ = _chunked_patch(port, uid, b"hello", "crc32 AAAA")
        assert status == 400
        assert _offset(port, uid) == 0

    def test_chunked_without_trailer_accepted(self, live_server):
        port, _ = live_server
        uid = _create_upload(port, 5)
        status, _ = _chunked_patch(port, uid, b"hello", None)
        assert status == 204
        assert _offset(port, uid) == 5

    def test_header_checksum_still_works_with_chunked_body(self, live_server):
        port, _ = live_server
        uid = _create_upload(port, 5)
        status, _ = _chunked_patch(
            port,
            uid,
            b"hello",
            None,
            extra_headers={"Upload-Checksum": f"sha1 {_sha1_b64(b'hello')}"},
        )
        assert status == 204
        assert _offset(port, uid) == 5

    def test_malformed_chunk_size_rejected_400(self, live_server):
        port, _ = live_server
        uid = _create_upload(port, 5)
        status, _ = _chunked_patch(port, uid, b"hello", None, chunk_size_line="zz")
        assert status == 400
        assert _offset(port, uid) == 0

    def test_chunked_body_exceeding_max_chunk_size_rejected_413(self, live_server):
        port, _ = live_server
        uid = _create_upload(port, 4096)
        big = b"x" * 2048  # fixture max_chunk_size=1024
        status, _ = _chunked_patch(port, uid, big, None)
        assert status == 413
        assert _offset(port, uid) == 0

    def test_chunked_creation_with_upload(self, live_server):
        port, storage = live_server
        data = b"hello"
        head = (
            "POST /files HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Tus-Resumable: 1.0.0\r\n"
            f"Upload-Length: {len(data)}\r\n"
            "Content-Type: application/offset+octet-stream\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        payload = head.encode("latin-1")
        payload += f"{len(data):x}\r\n".encode("latin-1") + data + b"\r\n0\r\n\r\n"
        status, headers = _raw(port, payload)
        assert status == 201
        assert headers["Upload-Offset"] == str(len(data))
        uid = headers["Location"].rsplit("/", 1)[1]
        assert storage.read_file(uid) == data


class TestAdvertisement:
    def test_advertised_when_enabled(self, live_server):
        port, _ = live_server
        req = urllib.request.Request(f"http://127.0.0.1:{port}/files", method="OPTIONS")
        with urllib.request.urlopen(req) as resp:
            extensions = resp.headers["Tus-Extension"].split(",")
        assert "checksum-trailer" in extensions

    def test_not_advertised_by_default(self, tmp_path):
        server = TusServer(
            storage=SQLiteStorage(
                db_path=str(tmp_path / "u.db"), upload_dir=str(tmp_path / "files")
            ),
            base_path="/files",
        )
        _, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert "checksum-trailer" not in headers["Tus-Extension"].split(",")


def test_cli_serve_enables_checksum_trailer(tmp_path, monkeypatch):
    """The bundled CLI transport parses trailers, so it must advertise them."""
    from resumable_upload import cli

    captured = {}

    class FakeThreadingHTTPServer:
        def __init__(self, addr, handler):
            captured["server"] = handler.tus_server
            self.server_address = (addr[0], 0)

        def serve_forever(self):
            raise KeyboardInterrupt  # immediately unwind serve loop

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "_ThreadingHTTPServer", FakeThreadingHTTPServer)
    monkeypatch.setattr(
        "sys.argv",
        [
            "resumable-upload",
            "serve",
            "--upload-dir",
            str(tmp_path / "up"),
            "--db-path",
            str(tmp_path / "u.db"),
        ],
    )
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        cli.main()
    assert captured["server"].supports_checksum_trailer is True
    assert "checksum-trailer" in captured["server"].SUPPORTED_EXTENSIONS
