"""Tests for client-side lifecycle hooks and retry refinements."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from http.server import HTTPServer

import pytest

from resumable_upload.client import TusClient
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def live_server():
    temp_dir = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(temp_dir, "u.db"),
        upload_dir=os.path.join(temp_dir, "files"),
    )
    tus = TusServer(storage=storage, base_path="/files")

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/files", storage
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)


class TestClientHooks:
    def test_before_request_and_after_response_fire(self, live_server, tmp_path):
        base_url, _ = live_server
        observed: list[tuple[str, str, int]] = []

        def before(method, url, headers):
            observed.append(("before", method, 0))

        def after(method, url, status):
            observed.append(("after", method, status))

        f = tmp_path / "x.bin"
        f.write_bytes(b"hello")

        client = TusClient(base_url, chunk_size=1024, before_request=before, after_response=after)
        client.upload_file(str(f))

        befores = [o for o in observed if o[0] == "before"]
        afters = [o for o in observed if o[0] == "after"]
        # At minimum: POST + one PATCH
        assert any(o[1] == "POST" for o in befores)
        assert any(o[1] == "PATCH" for o in befores)
        assert any(o[1] == "POST" and o[2] == 201 for o in afters)
        assert any(o[1] == "PATCH" and o[2] == 204 for o in afters)

    def test_on_should_retry_can_veto(self, tmp_path):
        """on_should_retry=False stops retrying after the first chunk failure."""
        from unittest.mock import MagicMock, patch
        from urllib.error import URLError

        from resumable_upload.client.uploader import Uploader

        attempts: list[int] = []

        def should_retry(err, attempt):
            attempts.append(attempt)
            return False

        f = tmp_path / "x.bin"
        f.write_bytes(b"hello")

        head_response = MagicMock()
        head_response.headers.get.return_value = "0"
        head_response.__enter__.return_value = head_response
        head_response.__exit__.return_value = False

        def urlopen_side_effect(req, **_kwargs):
            if req.get_method() == "HEAD":
                return head_response
            raise URLError("mocked PATCH failure")

        with patch("resumable_upload.client.uploader.urlopen", side_effect=urlopen_side_effect):
            uploader = Uploader(
                url="http://mocked.example/files/test",
                file_path=str(f),
                chunk_size=1024,
                max_retries=5,
                retry_delay=0.01,
                on_should_retry=should_retry,
                checksum=False,
            )
            from resumable_upload.exceptions import TusUploadFailed

            with pytest.raises(TusUploadFailed):
                uploader.upload()

        # Hook fired exactly once on the first PATCH failure; the remaining
        # 4 retries were skipped by the veto.
        assert attempts == [1]

    def test_find_previous_uploads_returns_stored_url(self, live_server, tmp_path):
        from resumable_upload.url_storage import FileURLStorage

        base_url, _ = live_server
        storage_path = tmp_path / "urls.json"
        url_storage = FileURLStorage(storage_path=str(storage_path))

        f = tmp_path / "foo.bin"
        f.write_bytes(b"0" * 128)

        client = TusClient(base_url, store_url=True, url_storage=url_storage)
        fingerprint = client.fingerprinter.get_fingerprint(str(f))
        created_url = client._create_upload(128, metadata={"filename": "foo.bin"})
        url_storage.set_url(fingerprint, created_url)

        previous = client.find_previous_uploads(str(f))
        assert len(previous) == 1
        assert previous[0]["upload_url"] == created_url
        assert previous[0]["fingerprint"] == fingerprint

    def test_find_previous_uploads_empty_when_disabled(self, live_server, tmp_path):
        base_url, _ = live_server
        client = TusClient(base_url)  # store_url=False
        f = tmp_path / "bar.bin"
        f.write_bytes(b"hello")
        assert client.find_previous_uploads(str(f)) == []

    def test_find_previous_uploads_none_when_missing(self, live_server, tmp_path):
        from resumable_upload.url_storage import FileURLStorage

        base_url, _ = live_server
        storage_path = tmp_path / "urls.json"
        url_storage = FileURLStorage(storage_path=str(storage_path))
        client = TusClient(base_url, store_url=True, url_storage=url_storage)
        f = tmp_path / "never-uploaded.bin"
        f.write_bytes(b"hi")
        assert client.find_previous_uploads(str(f)) == []
