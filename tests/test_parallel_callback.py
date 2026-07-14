"""Regression: on_upload_url_available must fire for parallel uploads too,
where the URL is only known after the partials merge into the final.
"""

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


class TestParallelUploadCallback:
    def test_on_upload_url_available_fires_for_parallel(self, live_server, tmp_path):
        port, storage = live_server
        seen: list[str] = []
        client = TusClient(
            f"http://127.0.0.1:{port}/files",
            chunk_size=1024,
            on_upload_url_available=seen.append,
        )
        p = tmp_path / "par.bin"
        p.write_bytes(b"Z" * 1000)

        url = client.upload_file(str(p), parallel_uploads=2)

        assert seen == [url]
        final_id = url.rsplit("/", 1)[1]
        assert storage.read_file(final_id) == b"Z" * 1000
