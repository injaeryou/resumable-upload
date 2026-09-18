"""stdlib BaseHTTPRequestHandler glue that delegates to ``TusServer``.

This is the synchronous on-the-wire entry point used by the bundled
``resumable-upload serve`` CLI. Frameworks that bring their own request
loop (Flask, FastAPI, Django, the ASGI adapter) bypass this class entirely
and call :meth:`TusServer.handle_request` directly.
"""

from __future__ import annotations

import shutil
import socketserver
from http.server import BaseHTTPRequestHandler
from typing import Any

from resumable_upload.server.server import TusServer

_MAX_CHUNK_SIZE_LINE = 8192
_MAX_TRAILER_LINE = 8192
_MAX_TRAILER_SECTION = 65536


class TusHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for TUS server."""

    tus_server: TusServer | None = None
    # Keep-alive: a chunked upload is many PATCHes, each one a fresh TCP (and
    # TLS) handshake under HTTP/1.0. Every response below is framed with
    # Content-Length so the next request can start on the same socket.
    protocol_version = "HTTP/1.1"

    def do_OPTIONS(self) -> None:
        """Handle OPTIONS request."""
        self._handle_request("OPTIONS")

    def do_GET(self) -> None:
        """Serve /metrics when a metrics registry is attached, else delegate.

        Non-metrics GETs go to the core, which serves downloads when
        ``enable_downloads`` is set and 404s otherwise.
        """
        if (
            self.tus_server is not None
            and self.tus_server.metrics is not None
            and self.path == self.tus_server.metrics_path
        ):
            framing = self._request_framing("GET")
            if framing is None:
                return
            body = self.tus_server.metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if framing[2]:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self._handle_request("GET")

    def do_POST(self) -> None:
        """Handle POST request."""
        self._handle_request("POST")

    def do_HEAD(self) -> None:
        """Handle HEAD request."""
        self._handle_request("HEAD")

    def do_PATCH(self) -> None:
        """Handle PATCH request."""
        self._handle_request("PATCH")

    def do_DELETE(self) -> None:
        """Handle DELETE request."""
        self._handle_request("DELETE")

    def setup(self) -> None:
        """Set socket read timeout from server config to guard against Slowloris."""
        super().setup()
        if self.tus_server and self.tus_server.request_timeout > 0:
            self.connection.settimeout(self.tus_server.request_timeout)
        # Keep-alive needs one thread per connection: a plain single-threaded
        # HTTPServer (the README's minimal example) would sit in readline() on
        # a parked socket and starve every other client until request_timeout.
        if not isinstance(self.server, socketserver.ThreadingMixIn):
            self.protocol_version = "HTTP/1.0"

    # A kept-alive socket parks in readline() between requests. A server that
    # exposes ``idle_connections`` (the CLI's does) can cut those on shutdown
    # instead of waiting ``request_timeout`` for each; in-flight requests are
    # not in the set, so they still drain. Once the server is ``closing``, a
    # connection that just finished a request must not park again — it
    # registers as idle first, then checks the flag, so it is either cut by
    # the shutdown sweep or sees the flag; there is no window in between.
    def handle_one_request(self) -> None:
        idle = getattr(self.server, "idle_connections", None)
        if idle is not None:
            idle.add(self.connection)
            if getattr(self.server, "closing", False):
                idle.discard(self.connection)
                self.close_connection = True
                return
        try:
            super().handle_one_request()
        finally:
            if idle is not None:
                idle.discard(self.connection)

    def parse_request(self) -> bool:
        # Runs right after the request line arrived: no longer idle.
        idle = getattr(self.server, "idle_connections", None)
        if idle is not None:
            idle.discard(self.connection)
        return super().parse_request()

    def _send_error(self, status: int, message: bytes) -> None:
        """Reject a request whose body has not been (fully) read.

        Every caller short-circuits before consuming the body, so the unread
        bytes would be parsed as the next request on a kept-alive socket.
        Close instead; ``send_header("Connection", "close")`` also flips
        ``close_connection`` so the handler loop exits after this response.
        """
        assert self.tus_server is not None
        self.send_response(status)
        self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
        self.send_header("Content-Length", str(len(message)))
        self.send_header("Connection", "close")
        # Transport-level rejections short-circuit before the core, so add CORS
        # here too — otherwise a browser can't read the status of a 400/413 that
        # the body parser or the Content-Length size gates produced: the
        # response is opaque cross-origin and surfaces as a network error.
        origin = self.headers.get("Origin")
        for key, value in self.tus_server._add_cors_headers({}, origin=origin).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":  # RFC 9110 §9.3.2; the header still describes it
            self.wfile.write(message)

    def _read_chunked_body(self, method: str) -> tuple[bytes, dict[str, str]] | None:
        """Parse a chunked request body plus its trailer section.

        Returns ``(body, trailers)`` or ``None`` after sending an error
        response (malformed encoding / size limits).
        """
        assert self.tus_server is not None
        max_size = self.tus_server.max_size
        max_chunk = self.tus_server.max_chunk_size
        body = bytearray()
        while True:
            # 8 KiB accommodates RFC-legal chunk extensions; a line that hits
            # the cap without a newline would desync the framing if we kept
            # parsing (the unread remainder would be consumed as chunk data),
            # so reject it outright.
            size_line = self.rfile.readline(_MAX_CHUNK_SIZE_LINE + 2)
            if len(size_line) > _MAX_CHUNK_SIZE_LINE and not size_line.endswith(b"\n"):
                self._send_error(400, b"Chunk size line too long")
                return None
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                self._send_error(400, b"Malformed chunked encoding")
                return None
            if size < 0:
                self._send_error(400, b"Malformed chunked encoding")
                return None
            if size == 0:
                break
            # Enforce limits from the declared size alone, before reading the
            # chunk body — a huge or hostile declaration must not buffer.
            if max_size > 0 and len(body) + size > max_size:
                self._send_error(413, b"Request entity too large")
                return None
            if method == "PATCH" and max_chunk > 0 and len(body) + size > max_chunk:
                self._send_error(413, b"Chunk exceeds maximum chunk size")
                return None
            remaining = size
            while remaining:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    self._send_error(400, b"Malformed chunked encoding")
                    return None
                body += chunk
                remaining -= len(chunk)
            if self.rfile.read(2) != b"\r\n":
                self._send_error(400, b"Malformed chunked encoding")
                return None

        trailers: dict[str, str] = {}
        trailer_bytes = 0
        while True:
            line = self.rfile.readline(_MAX_TRAILER_LINE + 2)
            if line in (b"\r\n", b"\n", b""):
                break
            if len(line) > _MAX_TRAILER_LINE and not line.endswith(b"\n"):
                self._send_error(400, b"Trailer line too long")
                return None
            # None of the body-size gates apply after the 0 chunk — cap the
            # trailer section itself so a hostile client cannot grow memory
            # without bound by streaming endless trailer lines.
            trailer_bytes += len(line)
            if trailer_bytes > _MAX_TRAILER_SECTION:
                self._send_error(400, b"Trailer section too large")
                return None
            if b":" in line:
                name, _, value = line.partition(b":")
                trailers[name.decode("latin-1").strip()] = value.decode("latin-1").strip()
        return bytes(body), trailers

    def _request_framing(self, method: str) -> tuple[str, str | None, bool] | None:
        """Decide how this request's body is delimited, before anything reads it.

        Returns ``(transfer_encoding, content_length, close)`` or ``None``
        after sending an error response. Anything whose end we can't find
        must not stay on a kept-alive socket, or its bytes are parsed as the
        next request (request smuggling). Repeated fields are read in full:
        a proxy in front may honour the one we would have skipped.
        """
        transfer_encoding = (
            ", ".join(self.headers.get_all("Transfer-Encoding") or []).lower().strip()
        )
        lengths = {v.strip() for v in self.headers.get_all("Content-Length") or []}
        if len(lengths) > 1:
            # RFC 9112 §6.3: differing Content-Length values are unrecoverable.
            self._send_error(400, b"Conflicting Content-Length headers")
            return None
        content_length = next(iter(lengths), None)
        if content_length is not None and not (
            content_length.isascii() and content_length.isdigit()
        ):
            # 1*DIGIT only: int() would take "+5" and "5_0", and "" would read
            # as "no body" — each one a different frame than a proxy sees.
            self._send_error(400, b"Invalid Content-Length header")
            return None
        if transfer_encoding and transfer_encoding != "chunked":
            # RFC 9112 §6.1/§6.3: chunked not last means the body has no known
            # end (400); any other coding stacked on chunked we can't decode (501).
            if transfer_encoding.rsplit(",", 1)[-1].strip() != "chunked":
                self._send_error(400, b"Transfer-Encoding must end with chunked")
            else:
                self._send_error(501, b"Unsupported Transfer-Encoding")
            return None
        close = False
        if transfer_encoding and content_length is not None:
            # Both framings present: chunked wins, but the peer and any proxy in
            # between may disagree on where the message ends (RFC 9112 §6.1).
            close = True
        if method not in ("POST", "PATCH") and (
            transfer_encoding or (content_length or "0") != "0"
        ):
            # Nothing reads a body for these methods.
            close = True
        return transfer_encoding, content_length, close

    def _handle_request(self, method: str) -> None:
        """Handle incoming request."""
        if self.tus_server is None:
            # Misconfigured handler: no body is read, so don't keep the socket.
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        # Read body for POST/PATCH
        body = b""
        trailers: dict[str, str] = {}
        framing = self._request_framing(method)
        if framing is None:
            return
        transfer_encoding, content_length_header, close = framing
        if method in ("POST", "PATCH") and transfer_encoding:
            parsed = self._read_chunked_body(method)
            if parsed is None:
                return
            body, trailers = parsed
        elif method in ("POST", "PATCH"):
            try:
                content_length = int(content_length_header or 0)
            except (ValueError, TypeError):
                self._send_error(400, b"Invalid Content-Length header")
                return
            if content_length < 0:
                self._send_error(400, b"Content-Length must not be negative")
                return
            max_size = self.tus_server.max_size
            if max_size > 0 and content_length > max_size:
                self._send_error(413, b"Request entity too large")
                return
            max_chunk = self.tus_server.max_chunk_size
            if method == "PATCH" and max_chunk > 0 and content_length > max_chunk:
                self._send_error(413, b"Chunk exceeds maximum chunk size")
                return
            if content_length > 0:
                body = self.rfile.read(content_length)

        # Convert headers to dict; a trailing Upload-Checksum (checksum-trailer
        # extension) is surfaced as a regular header for the core, which is
        # transport-agnostic. A header-level Upload-Checksum wins if both exist.
        headers = dict(self.headers)
        trailer_checksum = next(
            (v for k, v in trailers.items() if k.lower() == "upload-checksum"), None
        )
        if trailer_checksum is not None and not any(
            k.lower() == "upload-checksum" for k in headers
        ):
            headers["Upload-Checksum"] = trailer_checksum

        # Handle request
        status, response_headers, response_body = self.tus_server.handle_request(
            method, self.path, headers, body
        )

        # Send response. Every non-204 response is framed with Content-Length
        # or a keep-alive client either waits for EOF or reads the leftover
        # bytes as the next status line; streamed downloads set their own.
        # 204 must not carry one (RFC 9110 §8.6).
        is_bytes = isinstance(response_body, (bytes, bytearray))
        self.send_response(status)
        for key, value in response_headers.items():
            self.send_header(key, value)
        if status != 204 and not any(k.lower() == "content-length" for k in response_headers):
            self.send_header("Content-Length", str(len(response_body)) if is_bytes else "0")
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        # A HEAD response ends at the blank line whatever its headers say
        # (RFC 9110 §9.3.2), so any body written here would lead the next one.
        if is_bytes:
            if response_body and method != "HEAD":
                self.wfile.write(response_body)
        elif method == "HEAD":
            response_body.close()
        else:
            # GET download: stream the body instead of buffering it in RAM.
            try:
                shutil.copyfileobj(response_body, self.wfile, 64 * 1024)
            finally:
                response_body.close()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging."""
        pass
