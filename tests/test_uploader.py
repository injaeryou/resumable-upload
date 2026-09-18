"""Test suite for Uploader class."""

import os
import shutil
import ssl
import tempfile
from http.server import HTTPServer
from threading import Thread

import pytest

from resumable_upload.client import TusClient, Uploader
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


class TestUploader:
    """Tests for Uploader class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def test_file(self, temp_dir):
        """Create a test file."""
        file_path = os.path.join(temp_dir, "test_file.txt")
        with open(file_path, "wb") as f:
            f.write(b"Hello World! " * 1000)  # ~13KB file
        return file_path

    @pytest.fixture
    def server(self, temp_dir):
        """Start a test server."""
        db_path = os.path.join(temp_dir, "test.db")
        upload_dir = os.path.join(temp_dir, "uploads")
        storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
        tus_server = TusServer(storage=storage, base_path="/files")

        class CustomHandler(TusHTTPRequestHandler):
            pass

        CustomHandler.tus_server = tus_server

        server = HTTPServer(("127.0.0.1", 0), CustomHandler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield f"http://127.0.0.1:{port}/files", storage

        server.shutdown()

    @pytest.fixture
    def client(self, server):
        """Create a client instance."""
        url, _ = server
        return TusClient(url, chunk_size=1024)

    def test_uploader_init_with_file_path(self, test_file, server):
        """Test Uploader initialization with file path."""
        url, storage = server

        # Create upload first
        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        # Create uploader
        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        assert uploader.url == upload_url
        assert uploader.file_path == test_file
        assert uploader.file_size == os.path.getsize(test_file)
        assert uploader.chunk_size == 1024
        assert uploader.offset >= 0

        uploader.close()

    def test_uploader_init_with_file_stream(self, test_file, server):
        """Test Uploader initialization with file stream."""
        url, storage = server

        # Create upload first
        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        # Create uploader with file stream
        with open(test_file, "rb") as f:
            uploader = Uploader(url=upload_url, file_stream=f, chunk_size=1024)

            assert uploader.url == upload_url
            assert uploader.file_stream == f
            assert uploader.file_size == os.path.getsize(test_file)
            assert not uploader._owns_file

            uploader.close()

    def test_uploader_init_missing_file(self, server):
        """Test Uploader initialization with non-existent file."""
        url, storage = server

        with pytest.raises(FileNotFoundError):
            Uploader(url=f"{url}/nonexistent", file_path="/nonexistent/file.txt")

    def test_uploader_init_no_file(self, server):
        """Test Uploader initialization without file path or stream."""
        url, storage = server

        with pytest.raises(ValueError, match="Either file_path or file_stream"):
            Uploader(url=f"{url}/test")

    def test_uploader_context_manager(self, test_file, server):
        """Test Uploader as context manager."""
        url, storage = server

        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        # Use as context manager
        with Uploader(url=upload_url, file_path=test_file) as uploader:
            assert uploader.url == upload_url
            # File should be closed automatically

    def test_upload_chunk(self, test_file, server):
        """Test uploading a single chunk."""
        url, storage = server

        # Create upload
        client = TusClient(url)  # noqa: F841
        file_size = os.path.getsize(test_file)

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        headers = {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
        }
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        # Create uploader
        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        # Upload single chunk
        has_more = uploader.upload_chunk()

        assert uploader.offset == 1024
        assert has_more is True  # More chunks remain

        # Upload another chunk
        has_more = uploader.upload_chunk()

        assert uploader.offset == 2048
        assert has_more is True

        uploader.close()

    def test_upload_chunk_complete(self, test_file, server):
        """Test uploading chunks until complete."""
        url, storage = server

        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        # Create uploader (upload is already complete)
        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        # Try to upload chunk (should return False)
        has_more = uploader.upload_chunk()

        assert has_more is False
        assert uploader.is_complete is True

        uploader.close()

    def test_upload_all(self, test_file, server):
        """Test uploading entire file."""
        url, storage = server

        # Create upload
        client = TusClient(url)  # noqa: F841
        file_size = os.path.getsize(test_file)

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        headers = {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
        }
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        # Create uploader
        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        # Track progress
        progress_calls = []

        def progress_callback(stats):
            progress_calls.append((stats.uploaded_bytes, stats.total_bytes))

        # Upload all
        result_url = uploader.upload(progress_callback=progress_callback)

        assert result_url == upload_url
        assert uploader.is_complete is True
        assert len(progress_calls) > 0
        assert progress_calls[-1][0] == progress_calls[-1][1]

        uploader.close()

    def test_upload_with_stop_at(self, test_file, server):
        """Test uploading with stop_at parameter."""
        url, storage = server

        # Create upload
        client = TusClient(url)  # noqa: F841
        file_size = os.path.getsize(test_file)

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        headers = {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
        }
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        # Create uploader
        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        # Upload with stop_at
        stop_at = 2048
        uploader.upload(stop_at=stop_at)

        assert uploader.offset == stop_at
        assert uploader.is_complete is False

        uploader.close()

    def test_uploader_progress(self, test_file, server):
        """Test uploader progress property."""
        url, storage = server

        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        # Check progress
        stats = uploader.stats
        assert stats.uploaded_bytes == stats.total_bytes  # Already complete
        assert stats.total_bytes == os.path.getsize(test_file)

        uploader.close()

    def test_uploader_is_complete(self, test_file, server):
        """Test uploader is_complete property."""
        url, storage = server

        # Create incomplete upload
        client = TusClient(url)  # noqa: F841
        file_size = os.path.getsize(test_file)

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        headers = {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
        }
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        assert uploader.is_complete is False

        # Upload all
        uploader.upload()

        assert uploader.is_complete is True

        uploader.close()

    def test_uploader_with_custom_headers(self, test_file, server):
        """Test uploader with custom headers."""
        url, storage = server

        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        uploader = Uploader(
            url=upload_url,
            file_path=test_file,
            chunk_size=1024,
            headers={"Authorization": "Bearer token", "X-Custom": "value"},
        )

        assert uploader.headers == {"Authorization": "Bearer token", "X-Custom": "value"}

        uploader.close()

    def test_uploader_without_checksum(self, test_file, server):
        """Test uploader with checksum disabled."""
        url, storage = server

        client = TusClient(url)  # noqa: F841
        file_size = os.path.getsize(test_file)

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        headers = {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
        }
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024, checksum=False)

        assert uploader.checksum is False

        # Upload should work without checksum
        uploader.upload_chunk()

        uploader.close()

    def test_uploader_invalid_chunk_size(self, test_file, server):
        """Test uploader with invalid chunk size."""
        url, storage = server

        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        with pytest.raises(ValueError, match="chunk_size must be at least 1"):
            Uploader(url=upload_url, file_path=test_file, chunk_size=0)

        with pytest.raises(ValueError, match="chunk_size must be at least 1"):
            Uploader(url=upload_url, file_path=test_file, chunk_size=-1)

    # --- Phase 3: URLError handling ---

    def test_get_offset_network_error_raises_communication_error(self, test_file, server):
        """URLError during _get_offset raises TusCommunicationError."""
        from unittest.mock import patch
        from urllib.error import URLError

        from resumable_upload.exceptions import TusCommunicationError

        url, storage = server
        client = TusClient(url)
        upload_url = client.upload_file(test_file)

        uploader = Uploader(url=upload_url, file_path=test_file)

        urlopen_err = patch(
            "resumable_upload.client.uploader.Uploader._send", side_effect=URLError("network error")
        )
        with urlopen_err, pytest.raises(TusCommunicationError):
            uploader._get_offset()

        uploader.close()

    # --- Phase 4: timeout parameter ---

    def test_uploader_has_default_timeout(self, test_file, server):
        """Uploader has a default timeout of 30 seconds."""
        url, storage = server
        client = TusClient(url)
        upload_url = client.upload_file(test_file)
        uploader = Uploader(url=upload_url, file_path=test_file)
        assert uploader.timeout == 30.0
        uploader.close()

    # --- Phase 1.5: ssl_context passed to Uploader ---

    def test_ssl_context_passed_to_urlopen(self, test_file, server):
        """ssl_context from TusClient is passed through to Uploader."""
        url, storage = server

        client = TusClient(url, verify_tls_cert=False)
        # ssl_context should be set on the client
        assert client.ssl_context is not None
        assert client.ssl_context.check_hostname is False
        assert client.ssl_context.verify_mode == ssl.CERT_NONE

        # create_uploader should propagate ssl_context to the Uploader
        uploader = client.create_uploader(test_file)
        assert uploader.ssl_context is client.ssl_context

        uploader.close()

    def test_upload_chunk_retry_max_retries_respected(self, test_file, server):
        """upload_chunk retries exactly max_retries times before giving up."""
        import unittest.mock
        from urllib.error import URLError

        from resumable_upload.exceptions import TusUploadFailed

        url, _ = server
        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        file_size = os.path.getsize(test_file)
        headers = {"Tus-Resumable": "1.0.0", "Upload-Length": str(file_size)}
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        call_count = 0

        def failing_urlopen(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise URLError("always fails")

        max_retries = 2
        uploader = Uploader(
            url=upload_url,
            file_path=test_file,
            chunk_size=1024,
            max_retries=max_retries,
            retry_delay=0.0,
        )

        urlopen_patch = unittest.mock.patch(
            "resumable_upload.client.uploader.Uploader._send", side_effect=failing_urlopen
        )
        with urlopen_patch, pytest.raises(TusUploadFailed):
            uploader.upload_chunk()

        # Initial attempt + max_retries retries
        assert call_count == max_retries + 1
        uploader.close()

    def test_upload_chunk_retry_increments_chunks_retried_stat(self, test_file, server):
        """chunks_retried stat is incremented when a retry succeeds."""
        import unittest.mock
        from urllib.error import URLError
        from urllib.parse import urljoin
        from urllib.request import Request
        from urllib.request import urlopen as real_urlopen

        url, _ = server

        file_size = os.path.getsize(test_file)
        req_headers = {"Tus-Resumable": "1.0.0", "Upload-Length": str(file_size)}
        req = Request(url, headers=req_headers, method="POST")
        with real_urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        uploader = Uploader(
            url=upload_url,
            file_path=test_file,
            chunk_size=1024,
            max_retries=2,
            retry_delay=0.0,
        )
        attempt = 0

        def fail_once(req):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise URLError("first attempt fails")
            return Uploader._send(uploader, req)

        with unittest.mock.patch.object(uploader, "_send", side_effect=fail_once):
            uploader.upload_chunk()

        assert uploader.stats.chunks_retried == 1
        uploader.close()

    def test_upload_with_stop_at_larger_than_file_size(self, test_file, server):
        """upload(stop_at=N) where N > file_size clamps to file_size and completes normally."""
        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        url, _ = server

        file_size = os.path.getsize(test_file)
        headers = {"Tus-Resumable": "1.0.0", "Upload-Length": str(file_size)}
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)
        # stop_at larger than actual file — should complete without OSError
        uploader.upload(stop_at=file_size * 10)
        assert uploader.is_complete is True
        uploader.close()

    def test_upload_with_stop_at_less_than_current_offset_is_noop(self, test_file, server):
        """upload(stop_at=N) where N <= current offset does nothing."""
        url, _ = server
        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        file_size = os.path.getsize(test_file)
        headers = {"Tus-Resumable": "1.0.0", "Upload-Length": str(file_size)}
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)
        uploader.upload(stop_at=2048)
        assert uploader.offset == 2048

        # stop_at below current offset → no additional upload
        uploader.upload(stop_at=1024)
        assert uploader.offset == 2048  # unchanged
        uploader.close()

    def test_upload_chunk_raises_on_truncated_file(self, test_file, server):
        """upload_chunk() raises OSError if file is shorter than expected."""
        import unittest.mock

        url, storage = server

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        file_size = os.path.getsize(test_file)
        headers = {"Tus-Resumable": "1.0.0", "Upload-Length": str(file_size)}
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        uploader = Uploader(url=upload_url, file_path=test_file, chunk_size=1024)

        # Simulate a file that returns empty bytes unexpectedly
        read_patch = unittest.mock.patch.object(uploader._file_handle, "read", return_value=b"")
        with read_patch, pytest.raises(OSError, match="Unexpected end of file"):
            uploader.upload_chunk()

        uploader.close()

    @staticmethod
    def _create_upload(url: str, test_file: str) -> str:
        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        headers = {"Tus-Resumable": "1.0.0", "Upload-Length": str(os.path.getsize(test_file))}
        with urlopen(Request(url, headers=headers, method="POST")) as response:
            location = response.headers.get("Location")
            return urljoin(url, location) if not location.startswith("http") else location

    def test_upload_chunk_does_not_retry_client_errors(self, test_file, server):
        """A 4xx (other than 409/423/429) is deterministic; retrying only burns the backoff."""
        import unittest.mock
        from urllib.error import HTTPError

        from resumable_upload.exceptions import TusUploadFailed

        url, _ = server
        upload_url = self._create_upload(url, test_file)
        calls = 0

        def bad_request(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise HTTPError(upload_url, 400, "Bad Request", {}, None)  # type: ignore[arg-type]

        uploader = Uploader(url=upload_url, file_path=test_file, max_retries=3, retry_delay=0.0)
        patched = unittest.mock.patch(
            "resumable_upload.client.uploader.Uploader._send", side_effect=bad_request
        )
        with patched, pytest.raises(TusUploadFailed) as ei:
            uploader.upload_chunk()
        uploader.close()
        assert calls == 1
        assert ei.value.status_code == 400

    def test_upload_chunk_retries_423_locked(self, test_file, server):
        """423 Locked is transient (another writer holds the lock) and stays retriable."""
        import unittest.mock
        from urllib.error import HTTPError

        from resumable_upload.exceptions import TusUploadFailed

        url, _ = server
        upload_url = self._create_upload(url, test_file)
        calls = 0

        def locked(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise HTTPError(upload_url, 423, "Locked", {}, None)  # type: ignore[arg-type]

        uploader = Uploader(url=upload_url, file_path=test_file, max_retries=2, retry_delay=0.0)
        patched = unittest.mock.patch(
            "resumable_upload.client.uploader.Uploader._send", side_effect=locked
        )
        with patched, pytest.raises(TusUploadFailed):
            uploader.upload_chunk()
        uploader.close()
        assert calls == 3


@pytest.fixture
def counting_server(tmp_path):
    """Threaded TUS server that counts accepted TCP connections.

    Yields ``(base_url, handler_class, counts)``; tests may set
    ``handler_class.protocol_version`` before uploading.
    """
    from resumable_upload.cli import _ThreadingHTTPServer

    def make(request_timeout: int = 30):
        storage = SQLiteStorage(
            db_path=str(tmp_path / f"u{request_timeout}.db"),
            upload_dir=str(tmp_path / f"f{request_timeout}"),
        )
        tus = TusServer(storage=storage, base_path="/files", request_timeout=request_timeout)
        counts = {"connections": 0}

        class Handler(TusHTTPRequestHandler):
            def setup(self) -> None:
                counts["connections"] += 1
                super().setup()

        Handler.tus_server = tus
        httpd = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/files", Handler, counts

    servers: list = []
    try:
        yield make
    finally:
        for httpd in servers:
            httpd.shutdown()
            httpd.server_close()


class TestUploaderKeepAlive:
    """The sync Uploader keeps one connection for HEAD + every PATCH, and
    degrades to one connection per request against HTTP/1.0 peers."""

    @staticmethod
    def _file(tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"k" * 13_000)  # 13 chunks of 1024
        return str(f)

    def test_http11_server_gets_one_connection(self, counting_server, tmp_path):
        base, _, counts = counting_server()
        client = TusClient(base, chunk_size=1024)
        url = client.upload_file(self._file(tmp_path))
        assert client.get_upload_info(url)["complete"] is True
        # POST (urlopen) + HEAD/PATCH x13 (uploader) + HEAD (info) => 3 sockets
        assert counts["connections"] == 3

    def test_http10_server_reconnects_per_request(self, counting_server, tmp_path):
        base, handler, counts = counting_server()
        handler.protocol_version = "HTTP/1.0"
        client = TusClient(base, chunk_size=1024)
        url = client.upload_file(self._file(tmp_path))
        assert client.get_upload_info(url)["complete"] is True
        assert counts["connections"] == 1 + 1 + 13 + 1  # every request its own socket

    def test_stale_keepalive_socket_is_reopened_once(self, counting_server, tmp_path):
        import time

        base, _, counts = counting_server(request_timeout=1)
        client = TusClient(base, chunk_size=1024)

        def stall_after_first_chunk(stats):
            if stats.chunks_completed == 1:
                time.sleep(1.6)  # server's idle timeout reaps the kept-alive socket

        url = client.upload_file(self._file(tmp_path), progress_callback=stall_after_first_chunk)
        assert client.get_upload_info(url)["complete"] is True
        assert counts["connections"] == 4  # POST, uploader, reopened uploader, info

    def test_environment_proxy_falls_back_to_urlopen(self, counting_server, tmp_path, monkeypatch):
        from unittest.mock import patch
        from urllib.error import URLError

        from resumable_upload.exceptions import TusCommunicationError

        base, _, _ = counting_server()
        client = TusClient(base, chunk_size=1024)
        upload_url = client.upload_file(self._file(tmp_path))
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        monkeypatch.delenv("no_proxy", raising=False)
        calls = []

        def spy(req, **kwargs):
            calls.append(req.get_method())
            raise URLError("proxy unreachable")

        spied = patch("resumable_upload.client.uploader.urlopen", side_effect=spy)
        with spied, pytest.raises(TusCommunicationError):
            Uploader(url=upload_url, file_path=self._file(tmp_path))
        assert calls == ["HEAD"]

    def test_redirect_on_patch_is_an_error_not_a_silent_success(self, tmp_path):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from resumable_upload.exceptions import TusUploadFailed

        class Redirecting(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Upload-Offset", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_PATCH(self):  # noqa: N802
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(307)
                self.send_header("Location", "https://elsewhere.example/files/x")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # noqa: ARG002
                pass

        # Single-threaded server: the uploader's kept-alive socket must be
        # closed before shutdown() or serve_forever never returns.
        httpd = HTTPServer(("127.0.0.1", 0), Redirecting)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        uploader = None
        try:
            uploader = Uploader(
                url=f"http://127.0.0.1:{httpd.server_address[1]}/files/x",
                file_path=self._file(tmp_path),
                chunk_size=1024,
            )
            with pytest.raises(TusUploadFailed) as ei:
                uploader.upload_chunk()
            assert "307" in str(ei.value)
            assert uploader.offset == 0  # nothing was applied, nothing advanced
        finally:
            if uploader is not None:
                uploader.close()
            httpd.shutdown()
            httpd.server_close()


def _stub_server(handler_cls):
    """Threaded HTTP/1.1 stub; returns ``(base_url, httpd)``."""
    import threading
    from http.server import ThreadingHTTPServer

    handler_cls.protocol_version = "HTTP/1.1"
    handler_cls.log_message = lambda *args: None  # type: ignore[method-assign]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


class TestUploaderConnectionDetails:
    """What the persistent connection sends must match what urlopen sent."""

    @staticmethod
    def _file(tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"k" * 2048)
        return str(f)

    def _recording_handler(self, seen, patch_delay=0.0):
        import time
        from http.server import BaseHTTPRequestHandler

        class Recording(BaseHTTPRequestHandler):
            def do_HEAD(self):  # noqa: N802
                seen.append(("HEAD", self.headers.get("User-Agent")))
                self.send_response(200)
                self.send_header("Upload-Offset", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_PATCH(self):  # noqa: N802
                seen.append(("PATCH", self.headers.get("User-Agent")))
                self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(patch_delay)
                self.send_response(204)
                self.send_header("Upload-Offset", self.headers["Upload-Offset"])
                self.end_headers()

        return Recording

    def test_default_user_agent_matches_urlopen(self, tmp_path):
        import sys

        seen: list = []
        base, httpd = _stub_server(self._recording_handler(seen))
        try:
            with Uploader(url=f"{base}/files/x", file_path=self._file(tmp_path)) as up:
                up.upload_chunk()
        finally:
            httpd.shutdown()
            httpd.server_close()
        ua = "Python-urllib/{}.{}".format(*sys.version_info[:2])
        assert seen == [("HEAD", ua), ("PATCH", ua)]

    def test_custom_user_agent_wins(self, tmp_path):
        seen: list = []
        base, httpd = _stub_server(self._recording_handler(seen))
        try:
            with Uploader(
                url=f"{base}/files/x",
                file_path=self._file(tmp_path),
                headers={"user-agent": "mine/1"},
            ) as up:
                up.upload_chunk()
        finally:
            httpd.shutdown()
            httpd.server_close()
        assert seen == [("HEAD", "mine/1"), ("PATCH", "mine/1")]

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://[::1]/files/x", ("::1", 80)),
            ("https://[::1]/files/x", ("::1", 443)),
            ("http://[::1]:8080/files/x", ("::1", 8080)),
        ],
    )
    def test_ipv6_literal_host_and_default_port(self, tmp_path, url, expected):
        from unittest.mock import patch

        from resumable_upload.exceptions import TusCommunicationError

        calls = []

        def refuse(host, port, **kwargs):
            calls.append((host, port))
            raise ConnectionRefusedError

        name = "HTTPSConnection" if url.startswith("https") else "HTTPConnection"
        refused = patch(f"resumable_upload.client.uploader.{name}", side_effect=refuse)
        with refused, pytest.raises(TusCommunicationError):
            Uploader(url=url, file_path=self._file(tmp_path))
        assert calls == [expected]

    def test_timeout_is_not_silently_resent(self, tmp_path):
        from resumable_upload.exceptions import TusUploadFailed

        seen: list = []
        base, httpd = _stub_server(self._recording_handler(seen, patch_delay=3.0))
        try:
            with Uploader(url=f"{base}/files/x", file_path=self._file(tmp_path), timeout=1.0) as up:
                with pytest.raises(TusUploadFailed):
                    up.upload_chunk()
                assert [m for m, _ in seen] == ["HEAD", "PATCH"]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_redirected_head_is_an_error(self, tmp_path):
        """Following it would read the offset from one URL and PATCH another."""
        from http.server import BaseHTTPRequestHandler

        from resumable_upload.exceptions import TusCommunicationError

        seen: list = []

        class Moved(BaseHTTPRequestHandler):
            def do_HEAD(self):  # noqa: N802
                seen.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/new")
                self.send_header("Content-Length", "0")
                self.end_headers()

        base, httpd = _stub_server(Moved)
        try:
            with pytest.raises(TusCommunicationError) as ei:
                Uploader(url=f"{base}/old", file_path=self._file(tmp_path))
        finally:
            httpd.shutdown()
            httpd.server_close()
        assert ei.value.status_code == 307
        assert seen == ["/old"]
