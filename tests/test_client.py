"""Test suite for client module."""

import os
import shutil
import ssl
import tempfile
from http.server import HTTPServer
from threading import Thread

import pytest

from resumable_upload.client import TusClient
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage
from resumable_upload.url_storage import FileURLStorage


class TestTusClient:
    """Tests for TusClient."""

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
            f.write(b"Hello World! " * 1000)
        return file_path

    @pytest.fixture
    def server(self, temp_dir):
        """Start a test server."""
        db_path = os.path.join(temp_dir, "test.db")
        upload_dir = os.path.join(temp_dir, "uploads")
        storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
        tus_server = TusServer(storage=storage, base_path="/files")

        # Create custom handler class
        class CustomHandler(TusHTTPRequestHandler):
            pass

        CustomHandler.tus_server = tus_server

        # Start server in a thread
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

    def test_upload_file(self, client, test_file, server):
        """Test uploading a file."""
        url, storage = server

        # Track progress
        progress_calls = []

        def progress_callback(stats):
            progress_calls.append((stats.uploaded_bytes, stats.total_bytes))

        upload_url = client.upload_file(
            test_file,
            metadata={"filename": "test.txt"},
            progress_callback=progress_callback,
        )

        assert upload_url is not None
        assert upload_url.startswith(url)

        # Verify progress was tracked
        assert len(progress_calls) > 0
        assert progress_calls[-1][0] == progress_calls[-1][1]

        # Verify file was uploaded
        upload_id = upload_url.split("/")[-1]
        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["completed"] is True

    def test_upload_nonexistent_file(self, client):
        """Test uploading a non-existent file."""
        with pytest.raises(FileNotFoundError):
            client.upload_file("/nonexistent/file.txt")

    def test_resume_upload(self, client, test_file, server):
        """Test resuming an upload."""
        url, storage = server

        # Start upload
        file_size = os.path.getsize(test_file)

        # Create upload manually
        with open(test_file, "rb") as f:
            # Upload first chunk
            headers = {
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(file_size),
            }

            from urllib.request import Request, urlopen

            req = Request(url, headers=headers, method="POST")
            with urlopen(req) as response:
                location = response.headers.get("Location")
                # Handle relative URL
                if not location.startswith("http"):
                    from urllib.parse import urljoin

                    upload_url = urljoin(url, location)
                else:
                    upload_url = location

            # Upload partial data
            chunk = f.read(1024)
            headers = {
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
            req = Request(upload_url, data=chunk, headers=headers, method="PATCH")
            with urlopen(req) as response:
                pass

        # Resume upload
        upload_url_resumed = client.resume_upload(test_file, upload_url)
        assert upload_url_resumed == upload_url

        # Verify completion
        upload_id = upload_url.split("/")[-1]
        upload = storage.get_upload(upload_id)
        assert upload["completed"] is True

    def test_delete_upload(self, client, test_file, server):
        """Test deleting an upload."""
        url, storage = server

        # Upload file
        upload_url = client.upload_file(test_file)

        # Delete upload
        client.delete_upload(upload_url)

        # Verify deletion
        upload_id = upload_url.split("/")[-1]
        upload = storage.get_upload(upload_id)
        assert upload is None

    def test_client_without_checksum(self, test_file, server):
        """Test client with checksum disabled."""
        url, storage = server
        client = TusClient(url, checksum=False)

        upload_url = client.upload_file(test_file)
        assert upload_url is not None

        upload_id = upload_url.split("/")[-1]
        upload = storage.get_upload(upload_id)
        assert upload["completed"] is True

    def test_update_headers(self, client):
        """Test updating headers at runtime."""
        # Initially no custom headers
        headers = client.get_headers()
        assert headers == {}

        # Update headers
        client.update_headers({"Authorization": "Bearer token"})
        headers = client.get_headers()
        assert headers == {"Authorization": "Bearer token"}

        # Update with additional headers
        client.update_headers({"X-API-Key": "test-key"})
        headers = client.get_headers()
        assert headers == {"Authorization": "Bearer token", "X-API-Key": "test-key"}

        # Update existing header
        client.update_headers({"Authorization": "Bearer new-token"})
        headers = client.get_headers()
        assert headers["Authorization"] == "Bearer new-token"

    def test_get_headers(self, client):
        """Test getting current headers."""
        # Test initial headers
        headers = client.get_headers()
        assert isinstance(headers, dict)
        assert headers == {}

        # Set headers and verify
        client.update_headers({"Test-Header": "test-value"})
        headers = client.get_headers()
        assert headers == {"Test-Header": "test-value"}

        # Verify it's a copy (modifying shouldn't affect client)
        headers["New-Key"] = "new-value"
        client_headers = client.get_headers()
        assert "New-Key" not in client_headers

    def test_get_metadata(self, client, test_file, server):
        """Test getting upload metadata."""
        url, storage = server

        # Upload file with metadata
        upload_url = client.upload_file(
            test_file, metadata={"filename": "test.txt", "content-type": "text/plain"}
        )

        # Get metadata
        metadata = client.get_metadata(upload_url)
        assert metadata == {"filename": "test.txt", "content-type": "text/plain"}

    def test_get_metadata_empty(self, client, test_file, server):
        """Test getting metadata when no metadata exists."""
        url, storage = server

        # Upload file without metadata (but filename is auto-added)
        upload_url = client.upload_file(test_file, metadata={})

        # Get metadata (filename is auto-added by upload_file)
        metadata = client.get_metadata(upload_url)
        # filename is automatically added by upload_file when file_path is provided
        assert "filename" in metadata
        assert metadata["filename"] == os.path.basename(test_file)

    def test_get_server_info(self, client, server):
        """Test getting server information."""
        url, storage = server

        server_info = client.get_server_info()

        assert "version" in server_info
        assert server_info["version"] == "1.0.0"
        assert "extensions" in server_info
        assert isinstance(server_info["extensions"], list)
        assert "creation" in server_info["extensions"]
        assert "max_size" in server_info
        # max_size can be None if unlimited

    def test_get_upload_info(self, client, test_file, server):
        """Test getting upload information."""
        url, storage = server

        # Upload file
        upload_url = client.upload_file(
            test_file, metadata={"filename": "test.txt", "content-type": "text/plain"}
        )

        # Get upload info
        info = client.get_upload_info(upload_url)

        assert "offset" in info
        assert "length" in info
        assert "complete" in info
        assert "metadata" in info

        assert info["offset"] == info["length"]  # Upload is complete
        assert info["complete"] is True
        assert info["metadata"] == {"filename": "test.txt", "content-type": "text/plain"}

    def test_get_upload_info_partial(self, client, test_file, server):
        """Test getting upload information for partial upload."""
        url, storage = server

        # Create upload manually and upload partial data
        file_size = os.path.getsize(test_file)

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        # Create upload
        headers = {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
            "Upload-Metadata": "filename dGVzdC50eHQ=",  # base64("test.txt")
        }
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        # Upload partial data
        with open(test_file, "rb") as f:
            chunk = f.read(1024)
            headers = {
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
            req = Request(upload_url, data=chunk, headers=headers, method="PATCH")
            with urlopen(req):
                pass

        # Get upload info
        info = client.get_upload_info(upload_url)

        assert info["offset"] == 1024
        assert info["length"] == file_size
        assert info["complete"] is False
        assert info["metadata"]["filename"] == "test.txt"

    def test_create_uploader(self, client, test_file, server):
        """Test creating an uploader from client."""
        url, storage = server

        uploader = client.create_uploader(test_file, metadata={"filename": "test.txt"})

        assert uploader is not None
        assert uploader.url is not None
        assert uploader.url.startswith(url)
        assert uploader.file_size == os.path.getsize(test_file)
        assert uploader.offset >= 0

        uploader.close()

    def test_create_uploader_with_existing_url(self, client, test_file, server):
        """Test creating uploader with existing upload URL."""
        url, storage = server

        # Create upload first
        upload_url = client.upload_file(test_file)

        # Create uploader with existing URL
        uploader = client.create_uploader(test_file, upload_url=upload_url)

        assert uploader.url == upload_url
        assert uploader.is_complete is True

        uploader.close()

    # --- Phase 2.4: store_url auto-creates FileURLStorage ---

    def test_store_url_auto_creates_file_storage(self, server, temp_dir):
        """store_url=True without url_storage auto-creates FileURLStorage."""
        url, storage = server
        # Change working directory to temp_dir to avoid polluting project root
        original_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            client = TusClient(url, store_url=True)
            assert isinstance(client.url_storage, FileURLStorage)
        finally:
            os.chdir(original_cwd)
            # Clean up auto-created .tus_urls.json
            tus_file = os.path.join(temp_dir, ".tus_urls.json")
            if os.path.exists(tus_file):
                os.remove(tus_file)

    def test_fingerprint_calculated_once(self, test_file, server, temp_dir):
        """Fingerprint is calculated exactly once per upload_file call."""
        url, storage = server
        original_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            client = TusClient(url, store_url=True, chunk_size=1024)

            call_count = 0
            original_fp = client.fingerprinter.get_fingerprint

            def counting_fp(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                return original_fp(*args, **kwargs)

            client.fingerprinter.get_fingerprint = counting_fp
            client.upload_file(test_file)
            assert call_count == 1
        finally:
            os.chdir(original_cwd)
            tus_file = os.path.join(temp_dir, ".tus_urls.json")
            if os.path.exists(tus_file):
                os.remove(tus_file)

    # --- Phase 2.5: resume_upload with file_stream ---

    def test_resume_upload_with_file_stream(self, client, test_file, server):
        """resume_upload works with file_stream instead of file_path."""
        url, storage = server

        file_size = os.path.getsize(test_file)

        from urllib.parse import urljoin
        from urllib.request import Request, urlopen

        # Create upload and upload partial data
        headers = {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
        }
        req = Request(url, headers=headers, method="POST")
        with urlopen(req) as response:
            location = response.headers.get("Location")
            upload_url = urljoin(url, location) if not location.startswith("http") else location

        with open(test_file, "rb") as f:
            chunk = f.read(1024)
            headers = {
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
            req = Request(upload_url, data=chunk, headers=headers, method="PATCH")
            with urlopen(req):
                pass

        # Resume using file_stream
        with open(test_file, "rb") as f:
            result_url = client.resume_upload(upload_url=upload_url, file_stream=f)

        assert result_url == upload_url
        upload_id = upload_url.split("/")[-1]
        upload = storage.get_upload(upload_id)
        assert upload["completed"] is True

    # --- Phase 1.5: verify_tls_cert builds ssl_context ---

    # --- Phase 3: URLError handling ---

    def test_create_upload_network_error_raises_communication_error(self, server, test_file):
        """URLError during _create_upload raises TusCommunicationError."""
        from unittest.mock import patch
        from urllib.error import URLError

        from resumable_upload.exceptions import TusCommunicationError

        url, storage = server
        client = TusClient(url)

        urlopen_err = patch(
            "resumable_upload.client.client.urlopen", side_effect=URLError("network error")
        )
        with urlopen_err, pytest.raises(TusCommunicationError):
            client.upload_file(test_file)

    # --- Phase 1.5: verify_tls_cert builds ssl_context ---

    def test_verify_tls_cert_false_builds_ssl_context(self, server):
        """verify_tls_cert=False creates an ssl_context with cert checking disabled."""
        url, storage = server
        client = TusClient(url, verify_tls_cert=False)

        assert client.ssl_context is not None
        assert client.ssl_context.check_hostname is False
        assert client.ssl_context.verify_mode == ssl.CERT_NONE

    def test_verify_tls_cert_true_no_ssl_context(self, server):
        """verify_tls_cert=True (default) leaves ssl_context as None."""
        url, storage = server
        client = TusClient(url)
        assert client.ssl_context is None

    # --- Phase 4: timeout parameter ---

    def test_client_has_default_timeout(self, server):
        """TusClient has a default timeout of 30 seconds."""
        url, storage = server
        client = TusClient(url)
        assert client.timeout == 30.0

    def test_client_custom_timeout_passed_to_uploader(self, test_file, server):
        """Custom timeout on TusClient is passed through to Uploader."""
        url, storage = server
        client = TusClient(url, timeout=10.0)
        uploader = client.create_uploader(test_file)
        assert uploader.timeout == 10.0
        uploader.close()

    # --- Data integrity ---

    def test_upload_file_data_integrity(self, client, test_file, server):
        """Uploaded file content matches original file byte-for-byte."""
        url, storage = server

        with open(test_file, "rb") as f:
            original_data = f.read()

        upload_url = client.upload_file(test_file)
        upload_id = upload_url.split("/")[-1]
        uploaded_data = storage.read_file(upload_id)

        assert uploaded_data == original_data

    def test_upload_stream_data_integrity(self, test_file, server):
        """Uploaded stream content matches original bytes."""
        url, storage = server
        client = TusClient(url, chunk_size=1024)

        with open(test_file, "rb") as f:
            original_data = f.read()

        with open(test_file, "rb") as f:
            upload_url = client.upload_file(file_stream=f, metadata={"filename": "test.txt"})

        upload_id = upload_url.split("/")[-1]
        uploaded_data = storage.read_file(upload_id)
        assert uploaded_data == original_data

    # --- store_url cross-session resumability ---

    def test_upload_with_store_url_reuses_same_url(self, test_file, server, temp_dir):
        """With store_url=True, uploading the same file twice uses the same URL."""
        url, storage = server
        tus_file = os.path.join(temp_dir, ".tus_urls.json")
        url_storage = FileURLStorage(tus_file)
        client = TusClient(url, store_url=True, url_storage=url_storage, chunk_size=1024)

        url1 = client.upload_file(test_file)
        url2 = client.upload_file(test_file)

        assert url1 == url2

    def test_resume_with_stale_url_raises(self, client, test_file, server):
        """resume_upload with a URL that no longer exists raises TusCommunicationError."""
        from resumable_upload.exceptions import TusCommunicationError

        url, _ = server
        stale_url = f"{url}/00000000-0000-0000-0000-000000000099"

        with pytest.raises(TusCommunicationError):
            client.resume_upload(file_path=test_file, upload_url=stale_url)

    # --- Resource cleanup ---

    def test_upload_file_closes_handle_on_server_error(self, test_file, server):
        """File handle is closed even when upload raises an exception."""
        from unittest.mock import patch
        from urllib.error import URLError

        from resumable_upload.exceptions import TusCommunicationError

        url, _ = server
        client = TusClient(url)

        open_handles = []
        original_open = open

        def tracking_open(path, *args, **kwargs):
            fh = original_open(path, *args, **kwargs)
            open_handles.append(fh)
            return fh

        open_patch = patch("builtins.open", side_effect=tracking_open)
        urlopen_err = patch(
            "resumable_upload.client.client.urlopen", side_effect=URLError("network error")
        )
        with open_patch, urlopen_err, pytest.raises(TusCommunicationError):
            client.upload_file(test_file)

        for fh in open_handles:
            assert fh.closed, "File handle was not closed after exception"
