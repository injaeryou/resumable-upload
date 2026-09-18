"""Raw-socket tests for response framing on a kept-alive stdlib connection:
every response must be delimited exactly, and a request body the handler
does not read must never be parsed as the next request.
"""

import os
import shutil
import socket
import tempfile
import threading
from http.server import ThreadingHTTPServer

import pytest

from resumable_upload.exceptions import TusHookError
from resumable_upload.metrics import MetricsRegistry
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage

T = b"Host: x\r\nTus-Resumable: 1.0.0\r\n"
OPTIONS = b"OPTIONS /files HTTP/1.1\r\n" + T + b"\r\n"


@pytest.fixture
def live_server():
    temp_dir = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(temp_dir, "u.db"),
        upload_dir=os.path.join(temp_dir, "files"),
    )
    seen = []

    def on_incoming_request(method, path, headers):
        seen.append((method, path))

    def on_before_terminate(upload_id):
        raise TusHookError("vetoed", status_code=403)

    tus = TusServer(
        storage=storage,
        base_path="/files",
        on_incoming_request=on_incoming_request,
        on_before_terminate=on_before_terminate,
        metrics_registry=MetricsRegistry(),
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _exchange(port: int, payload: bytes) -> list[tuple[str, dict, bytes]]:
    """Send ``payload`` on one socket, half-close, and split every response
    the server wrote by the framing it declared (Content-Length)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        raw = b""
        while True:
            got = sock.recv(65536)
            if not got:
                break
            raw += got
    responses = []
    while raw:
        head, sep, raw = raw.partition(b"\r\n\r\n")
        assert sep, f"unterminated response head: {head!r}"
        status_line, *lines = head.decode("latin-1").split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines)}
        # The caller knows which requests were HEAD; bodies are read by length.
        length = int(headers.get("content-length", "0"))
        responses.append((status_line, headers, raw[:length]))
        raw = raw[length:]
    return responses


def _head_ok(responses, n):
    """``n`` responses, each starting with a well-formed status line."""
    assert len(responses) == n, responses
    for status_line, _, _ in responses:
        assert status_line.startswith("HTTP/1.1 "), status_line


class TestErrorResponsesKeepFraming:
    def test_error_body_is_counted_in_content_length(self, live_server):
        port, _ = live_server
        bad = b"PATCH /files/not-a-uuid HTTP/1.1\r\n" + T + b"Content-Length: 0\r\n\r\n"
        responses = _exchange(port, bad + OPTIONS)
        _head_ok(responses, 2)
        assert responses[0][0].startswith("HTTP/1.1 400")
        assert responses[0][2] == b"Invalid upload ID format"
        assert responses[1][0].startswith("HTTP/1.1 204")

    def test_head_error_carries_no_body(self, live_server):
        port, _ = live_server
        head = b"HEAD /files/not-a-uuid HTTP/1.1\r\n" + T + b"\r\n"
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(head + OPTIONS)
            sock.shutdown(socket.SHUT_WR)
            raw = b""
            while True:
                got = sock.recv(65536)
                if not got:
                    break
                raw += got
        first, _, rest = raw.partition(b"\r\n\r\n")
        assert first.startswith(b"HTTP/1.1 400")
        # Nothing may sit between the HEAD response head and the next status line.
        assert rest.startswith(b"HTTP/1.1 204"), rest[:60]

    def test_hook_rejection_keeps_framing(self, live_server):
        port, _ = live_server
        create = b"POST /files HTTP/1.1\r\n" + T + b"Upload-Length: 5\r\nContent-Length: 0\r\n\r\n"
        [(_, headers, _)] = _exchange(port, create)
        path = "/files/" + headers["location"].rsplit("/", 1)[1]
        delete = b"DELETE " + path.encode() + b" HTTP/1.1\r\n" + T + b"\r\n"
        responses = _exchange(port, delete + OPTIONS)
        _head_ok(responses, 2)
        assert responses[0][0].startswith("HTTP/1.1 403")
        assert responses[1][0].startswith("HTTP/1.1 204")


class TestUnreadBodiesAreNotRequests:
    @pytest.mark.parametrize("method", [b"DELETE", b"HEAD", b"GET", b"OPTIONS"])
    def test_body_on_bodiless_method_is_not_smuggled(self, live_server, method):
        port, seen = live_server
        inner = b"OPTIONS /files/SMUGGLED HTTP/1.1\r\n" + T + b"\r\n"
        payload = (
            method
            + b" /files/00000000-0000-0000-0000-000000000000 HTTP/1.1\r\n"
            + T
            + b"Content-Length: %d\r\n\r\n" % len(inner)
            + inner
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(payload)
            raw = b""
            while True:
                got = sock.recv(65536)
                if not got:
                    break
                raw += got
        assert raw.count(b"HTTP/1.1 ") == 1, raw
        assert all("SMUGGLED" not in path for _, path in seen), seen

    @pytest.mark.parametrize("method", [b"DELETE", b"GET", b"POST"])
    def test_conflicting_content_lengths_rejected(self, live_server, method):
        """RFC 9112 §6.3: a proxy honouring the other field would desync (CL.CL)."""
        port, seen = live_server
        inner = b"OPTIONS /files/SMUGGLED HTTP/1.1\r\n" + T + b"\r\n"
        payload = (
            method
            + b" /files/00000000-0000-0000-0000-000000000000 HTTP/1.1\r\n"
            + T
            + b"Content-Length: 0\r\nContent-Length: %d\r\n\r\n" % len(inner)
            + inner
        )
        responses = _exchange(port, payload)
        _head_ok(responses, 1)
        assert responses[0][0].startswith("HTTP/1.1 400")
        assert responses[0][1].get("connection", "").lower() == "close"
        assert all("SMUGGLED" not in path for _, path in seen), seen

    @pytest.mark.parametrize("method", [b"DELETE", b"POST"])
    @pytest.mark.parametrize("value", [b"", b" ", b"\t", b"+64", b"6_4", b"0x40", b"64, 64"])
    def test_content_length_must_be_digits(self, live_server, method, value):
        """``int()`` takes "+64" and "6_4", and "" reads as no body; a proxy that
        frames the message differently would leave the body on the socket."""
        port, seen = live_server
        inner = (b"OPTIONS /files/SMUGGLED HTTP/1.1\r\n" + T + b"\r\n").ljust(64)
        payload = (
            method
            + b" /files/00000000-0000-0000-0000-000000000000 HTTP/1.1\r\n"
            + T
            + b"Content-Length: "
            + value
            + b"\r\n\r\n"
            + inner
        )
        responses = _exchange(port, payload)
        _head_ok(responses, 1)
        assert responses[0][0].startswith("HTTP/1.1 400")
        assert responses[0][1].get("connection", "").lower() == "close"
        assert all("SMUGGLED" not in path for _, path in seen), seen

    def test_transport_rejection_of_head_carries_no_body(self, live_server):
        port, _ = live_server
        head = (
            b"HEAD /files/00000000-0000-0000-0000-000000000000 HTTP/1.1\r\n"
            + T
            + b"Transfer-Encoding: gzip\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(head)
            sock.shutdown(socket.SHUT_WR)
            raw = b""
            while True:
                got = sock.recv(65536)
                if not got:
                    break
                raw += got
        first, _, rest = raw.partition(b"\r\n\r\n")
        assert first.startswith(b"HTTP/1.1 400")
        assert rest == b""

    def test_repeated_identical_content_length_accepted(self, live_server):
        port, _ = live_server
        payload = (
            b"POST /files HTTP/1.1\r\n"
            + T
            + b"Upload-Length: 5\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n"
        )
        responses = _exchange(port, payload)
        _head_ok(responses, 1)
        assert responses[0][0].startswith("HTTP/1.1 201")

    def test_transfer_encoding_split_across_fields_rejected(self, live_server):
        """``chunked`` then ``gzip`` is one list, ``chunked, gzip``: chunked is not last."""
        port, _ = live_server
        payload = (
            b"POST /files HTTP/1.1\r\n"
            + T
            + b"Upload-Length: 5\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: gzip\r\n\r\n"
            + b"0\r\n\r\n"
        )
        responses = _exchange(port, payload)
        _head_ok(responses, 1)
        assert responses[0][0].startswith("HTTP/1.1 400")

    def test_metrics_endpoint_does_not_leave_a_body_on_the_socket(self, live_server):
        port, seen = live_server
        inner = b"OPTIONS /files/SMUGGLED HTTP/1.1\r\n" + T + b"\r\n"
        payload = (
            b"GET /metrics HTTP/1.1\r\n" + T + b"Content-Length: %d\r\n\r\n" % len(inner) + inner
        )
        responses = _exchange(port, payload)
        _head_ok(responses, 1)
        assert responses[0][0].startswith("HTTP/1.1 200")
        assert responses[0][1].get("connection", "").lower() == "close"
        assert all("SMUGGLED" not in path for _, path in seen), seen

    def test_content_length_with_chunked_closes(self, live_server):
        """RFC 9112 §6.1: Transfer-Encoding wins, but the connection must close."""
        port, _ = live_server
        payload = (
            b"POST /files HTTP/1.1\r\n"
            + T
            + b"Upload-Length: 5\r\nTransfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n"
            + b"0\r\n\r\n"
            + OPTIONS
        )
        responses = _exchange(port, payload)
        _head_ok(responses, 1)
        assert responses[0][0].startswith("HTTP/1.1 201")
        assert responses[0][1].get("connection", "").lower() == "close"

    def test_unknown_transfer_coding_rejected(self, live_server):
        """A body framed by a coding we can't decode has no known end."""
        port, _ = live_server
        payload = (
            b"POST /files HTTP/1.1\r\n"
            + T
            + b"Upload-Length: 5\r\nTransfer-Encoding: gzip\r\n\r\n"
            + OPTIONS
        )
        responses = _exchange(port, payload)
        _head_ok(responses, 1)
        assert responses[0][0].startswith("HTTP/1.1 400")
        assert responses[0][1].get("connection", "").lower() == "close"
