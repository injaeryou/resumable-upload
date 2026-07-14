"""Raw-socket tests for chunked-framing hardening on the stdlib transport:
an over-long chunk-size line and an unbounded trailer section must both be
rejected with 400 rather than desyncing the framing or exhausting memory.
"""

import os
import shutil
import socket
import tempfile
import threading
from http.server import HTTPServer

import pytest

from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def live_server():
    temp_dir = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(temp_dir, "u.db"),
        upload_dir=os.path.join(temp_dir, "files"),
    )
    tus = TusServer(storage=storage, base_path="/files", supports_checksum_trailer=True)

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


def _raw_status(port: int, payload: bytes) -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        sock.settimeout(5)
        data = b""
        while b"\r\n" not in data:
            got = sock.recv(65536)
            if not got:
                break
            data += got
    return int(data.split(b"\r\n", 1)[0].split()[1])


def _chunked_post_prefix(port: int) -> bytes:
    return (
        b"POST /files HTTP/1.1\r\n"
        b"Host: 127.0.0.1:%d\r\n"
        b"Tus-Resumable: 1.0.0\r\n"
        b"Upload-Length: 5\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" % port
    )


class TestChunkedFramingHardening:
    def test_oversized_chunk_size_line_rejected_400(self, live_server):
        port, _ = live_server
        # RFC-legal but absurdly long chunk-extension: must be a clean 400,
        # never a framing desync that stores garbage bytes.
        payload = _chunked_post_prefix(port) + (b"5;ext=" + b"x" * 9000 + b"\r\nhello\r\n0\r\n\r\n")
        assert _raw_status(port, payload) == 400

    def test_reasonable_chunk_extension_accepted(self, live_server):
        port, _ = live_server
        payload = _chunked_post_prefix(port) + (b"5;ext=token\r\nhello\r\n0\r\n\r\n")
        assert _raw_status(port, payload) == 201

    def test_trailer_flood_rejected_400(self, live_server):
        port, _ = live_server
        flood = b"".join(b"a%d: x\r\n" % i for i in range(9000))
        payload = _chunked_post_prefix(port) + b"5\r\nhello\r\n0\r\n" + flood + b"\r\n"
        assert _raw_status(port, payload) == 400

    def test_oversized_trailer_line_rejected_400(self, live_server):
        port, _ = live_server
        payload = (
            _chunked_post_prefix(port) + b"5\r\nhello\r\n0\r\n" + b"a: " + b"x" * 9000 + b"\r\n\r\n"
        )
        assert _raw_status(port, payload) == 400

    def test_normal_trailers_still_accepted(self, live_server):
        port, _ = live_server
        payload = _chunked_post_prefix(port) + (b"5\r\nhello\r\n0\r\nX-Extra: 1\r\n\r\n")
        assert _raw_status(port, payload) == 201
