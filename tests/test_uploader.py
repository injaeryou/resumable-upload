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
            "resumable_upload.client.uploader.urlopen", side_effect=URLError("network error")
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
            "resumable_upload.client.uploader.urlopen", side_effect=failing_urlopen
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

        attempt = 0

        def fail_once(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise URLError("first attempt fails")
            return real_urlopen(*args, **kwargs)

        uploader = Uploader(
            url=upload_url,
            file_path=test_file,
            chunk_size=1024,
            max_retries=2,
            retry_delay=0.0,
        )

        urlopen_once = unittest.mock.patch(
            "resumable_upload.client.uploader.urlopen", side_effect=fail_once
        )
        with urlopen_once:
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
            "resumable_upload.client.uploader.urlopen", side_effect=bad_request
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
            "resumable_upload.client.uploader.urlopen", side_effect=locked
        )
        with patched, pytest.raises(TusUploadFailed):
            uploader.upload_chunk()
        uploader.close()
        assert calls == 3
