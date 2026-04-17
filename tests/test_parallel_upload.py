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


class TestParallelUploads:
    """Automatic parallel uploads via upload_file(parallel_uploads=N)."""

    def test_parallel_upload_roundtrip_equal_slices(self, live_server, tmp_path):
        base_url, storage = live_server
        big = tmp_path / "big.bin"
        # 1 MiB of deterministic data — exercises multiple chunks per slice
        payload = bytes((i * 7 + 3) % 256 for i in range(1024 * 1024))
        big.write_bytes(payload)

        client = TusClient(base_url, chunk_size=64 * 1024)
        final_url = client.upload_file(
            str(big),
            metadata={"filename": "big.bin"},
            parallel_uploads=4,
        )

        info = client.get_upload_info(final_url)
        assert info["length"] == len(payload)
        assert info["offset"] == len(payload)
        assert info["complete"] is True
        assert info["metadata"]["filename"] == "big.bin"

        final_id = final_url.rsplit("/", 1)[1]
        assert storage.read_file(final_id) == payload

    def test_parallel_upload_handles_uneven_slices(self, live_server, tmp_path):
        """Length not divisible by parallel_uploads: last slice gets remainder."""
        base_url, storage = live_server
        # 1001 bytes across 4 slices → 250/250/250/251
        payload = bytes(range(256)) * 3 + b"\xaa" * (1001 - 256 * 3)
        assert len(payload) == 1001
        f = tmp_path / "uneven.bin"
        f.write_bytes(payload)

        client = TusClient(base_url, chunk_size=128)
        final_url = client.upload_file(str(f), parallel_uploads=4)

        final_id = final_url.rsplit("/", 1)[1]
        assert storage.read_file(final_id) == payload

    def test_parallel_upload_rejects_invalid_n(self, live_server, tmp_path):
        base_url, _ = live_server
        f = tmp_path / "x.bin"
        f.write_bytes(b"hello")
        client = TusClient(base_url)
        with pytest.raises(ValueError, match="parallel_uploads"):
            client.upload_file(str(f), parallel_uploads=0)
        with pytest.raises(ValueError, match="parallel_uploads"):
            client.upload_file(str(f), parallel_uploads=-3)

    def test_parallel_upload_requires_file_path(self, live_server):
        from io import BytesIO

        base_url, _ = live_server
        client = TusClient(base_url)
        with pytest.raises(ValueError, match="file_path"):
            client.upload_file(file_stream=BytesIO(b"x" * 100), parallel_uploads=2)

    def test_parallel_upload_incompatible_with_stop_at(self, live_server, tmp_path):
        base_url, _ = live_server
        f = tmp_path / "x.bin"
        f.write_bytes(b"hello world")
        client = TusClient(base_url)
        with pytest.raises(ValueError, match="stop_at"):
            client.upload_file(str(f), parallel_uploads=2, stop_at=5)

    def test_parallel_upload_n_greater_than_size_degenerates(self, live_server, tmp_path):
        """parallel_uploads > file_size: skip empty slices, still succeeds."""
        base_url, storage = live_server
        f = tmp_path / "tiny.bin"
        f.write_bytes(b"abc")

        client = TusClient(base_url, chunk_size=1)
        final_url = client.upload_file(str(f), parallel_uploads=10)
        final_id = final_url.rsplit("/", 1)[1]
        assert storage.read_file(final_id) == b"abc"

    def test_parallel_upload_of_single_returns_normal_upload(self, live_server, tmp_path):
        """parallel_uploads=1 must behave exactly like the existing path."""
        base_url, storage = live_server
        f = tmp_path / "x.bin"
        f.write_bytes(b"regular-upload")

        client = TusClient(base_url, chunk_size=1024)
        url = client.upload_file(str(f), parallel_uploads=1)
        # parallel_uploads=1 uses the single-stream path — not a final upload —
        # so the returned URL is the upload itself, not a concatenated one.
        upload_id = url.rsplit("/", 1)[1]
        stored = storage.get_upload(upload_id)
        assert stored["is_partial"] is False
        assert storage.read_file(upload_id) == b"regular-upload"
