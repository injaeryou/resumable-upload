"""Tests for client-side partial/final and parallel upload support."""

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
    """Spin up an in-process TUS server and yield (base_url, storage)."""
    temp_dir = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(temp_dir, "u.db"),
        upload_dir=os.path.join(temp_dir, "files"),
    )
    tus = TusServer(storage=storage, base_path="/files")

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/files", storage
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)


class TestPartialFinalHelpers:
    def test_create_partial_upload_returns_url(self, live_server, tmp_path):
        base_url, storage = live_server
        client = TusClient(base_url, chunk_size=1024)

        p_path = tmp_path / "a.bin"
        p_path.write_bytes(b"A" * 100)

        url = client.create_partial_upload(str(p_path))
        assert url.startswith(base_url + "/")
        upload_id = url.rsplit("/", 1)[1]
        stored = storage.get_upload(upload_id)
        assert stored is not None
        assert stored["is_partial"] is True
        assert stored["offset"] == 100  # fully uploaded

    def test_create_partial_upload_from_stream(self, live_server):
        from io import BytesIO

        base_url, storage = live_server
        client = TusClient(base_url, chunk_size=1024)

        url = client.create_partial_upload(file_stream=BytesIO(b"hello"))
        upload_id = url.rsplit("/", 1)[1]
        assert storage.get_upload(upload_id)["is_partial"] is True

    def test_create_partial_upload_requires_source(self, live_server):
        base_url, _ = live_server
        client = TusClient(base_url)
        with pytest.raises(ValueError, match="file_path or file_stream"):
            client.create_partial_upload()

    def test_create_final_upload_merges_partials(self, live_server, tmp_path):
        base_url, storage = live_server
        client = TusClient(base_url, chunk_size=1024)

        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"A" * 100)
        b.write_bytes(b"B" * 200)

        url1 = client.create_partial_upload(str(a))
        url2 = client.create_partial_upload(str(b))

        final_url = client.create_final_upload(
            partial_urls=[url1, url2], metadata={"filename": "merged.bin"}
        )

        info = client.get_upload_info(final_url)
        assert info["length"] == 300
        assert info["offset"] == 300
        assert info["complete"] is True

        final_id = final_url.rsplit("/", 1)[1]
        assert storage.read_file(final_id) == b"A" * 100 + b"B" * 200

    def test_create_final_upload_requires_partial_urls(self, live_server):
        base_url, _ = live_server
        client = TusClient(base_url)
        with pytest.raises(ValueError, match="partial_urls"):
            client.create_final_upload(partial_urls=[])

    def test_create_final_upload_propagates_server_errors(self, live_server, tmp_path):
        """Server rejects final on incomplete partial → client raises."""
        base_url, storage = live_server
        client = TusClient(base_url, chunk_size=1024)

        # Manually create an incomplete partial via the storage
        incomplete_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        storage.create_upload(incomplete_id, 10, {}, is_partial=True)
        storage.write_chunk(incomplete_id, 0, b"abc")
        storage.update_offset(incomplete_id, 3)

        from resumable_upload.exceptions import TusCommunicationError

        with pytest.raises(TusCommunicationError):
            client.create_final_upload(partial_urls=[f"{base_url}/{incomplete_id}"], metadata={})
