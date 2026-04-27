"""stdlib BaseHTTPRequestHandler glue that delegates to ``TusServer``.

This is the synchronous on-the-wire entry point used by the bundled
``resumable-upload serve`` CLI. Frameworks that bring their own request
loop (Flask, FastAPI, Django, the ASGI adapter) bypass this class entirely
and call :meth:`TusServer.handle_request` directly.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from typing import Any

from resumable_upload.server.server import TusServer


class TusHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for TUS server."""

    tus_server: TusServer | None = None

    def do_OPTIONS(self) -> None:
        """Handle OPTIONS request."""
        self._handle_request("OPTIONS")

    def do_GET(self) -> None:
        """Serve /metrics when a metrics registry is attached; otherwise 404."""
        if (
            self.tus_server is not None
            and self.tus_server.metrics is not None
            and self.path == self.tus_server.metrics_path
        ):
            body = self.tus_server.metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

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

    def _handle_request(self, method: str) -> None:
        """Handle incoming request."""
        if self.tus_server is None:
            self.send_response(500)
            self.end_headers()
            return
        # Read body for POST/PATCH
        body = b""
        if method in ("POST", "PATCH"):
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                self.send_response(400)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Invalid Content-Length header")
                return
            if content_length < 0:
                self.send_response(400)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Content-Length must not be negative")
                return
            max_size = self.tus_server.max_size
            if max_size > 0 and content_length > max_size:
                self.send_response(413)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Request entity too large")
                return
            max_chunk = self.tus_server.max_chunk_size
            if method == "PATCH" and max_chunk > 0 and content_length > max_chunk:
                self.send_response(413)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Chunk exceeds maximum chunk size")
                return
            if content_length > 0:
                body = self.rfile.read(content_length)

        # Convert headers to dict
        headers = dict(self.headers)

        # Handle request
        status, response_headers, response_body = self.tus_server.handle_request(
            method, self.path, headers, body
        )

        # Send response
        self.send_response(status)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging."""
        pass
