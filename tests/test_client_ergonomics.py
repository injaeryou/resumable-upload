"""Tests for client ergonomics: override_patch_method, add_request_id,
on_upload_url_available, metadata_for_partial_uploads.

A live in-process server records every incoming request via the
on_incoming_request hook so tests can assert what actually hit the wire.
"""

import os
import re
import shutil
import tempfile
import threading
from http.server import HTTPServer

import pytest

from resumable_upload import TusClient
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def live_server():
    """Yield (base_url, storage, requests) where requests is a recorded list."""
    temp_dir = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(temp_dir, "u.db"),
        upload_dir=os.path.join(temp_dir, "files"),
    )
    requests: list[tuple[str, str, dict]] = []

    def record(method, path, headers):
        requests.append((method, path, dict(headers)))

    tus = TusServer(storage=storage, base_path="/files", on_incoming_request=record)

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/files", storage, requests
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def payload_file(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"hello ergonomic world")
    return str(p)


def _header(headers: dict, name: str):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


class TestOverridePatchMethod:
    def test_patch_sent_as_post_with_override(self, live_server, payload_file):
        base_url, storage, requests = live_server
        client = TusClient(base_url, override_patch_method=True)
        url = client.upload_file(payload_file)

        upload_id = url.rsplit("/", 1)[1]
        assert storage.read_file(upload_id) == b"hello ergonomic world"

        # The server hook sees the method *after* the override rewrite, so the
        # wire-level proof is the X-HTTP-Method-Override header on every
        # data-carrying request the server processed as PATCH.
        patches = [h for m, _, h in requests if m == "PATCH"]
        assert patches, "upload must have sent at least one data request"
        assert all(_header(h, "X-HTTP-Method-Override") == "PATCH" for h in patches)

    def test_default_still_uses_patch(self, live_server, payload_file):
        base_url, _, requests = live_server
        client = TusClient(base_url)
        client.upload_file(payload_file)
        patches = [h for m, _, h in requests if m == "PATCH"]
        assert patches
        assert all(_header(h, "X-HTTP-Method-Override") is None for h in patches)


class TestAddRequestId:
    def test_every_request_carries_unique_uuid(self, live_server, payload_file):
        base_url, _, requests = live_server
        client = TusClient(base_url, add_request_id=True, chunk_size=8)
        client.upload_file(payload_file)

        ids = [_header(h, "X-Request-ID") for _, _, h in requests]
        assert all(ids), f"missing X-Request-ID in some requests: {requests}"
        assert all(_UUID_RE.match(i) for i in ids)
        assert len(set(ids)) == len(ids), "request ids must be unique per request"

    def test_disabled_by_default(self, live_server, payload_file):
        base_url, _, requests = live_server
        TusClient(base_url).upload_file(payload_file)
        assert all(_header(h, "X-Request-ID") is None for _, _, h in requests)

    def test_user_supplied_header_not_clobbered(self, live_server, payload_file):
        base_url, _, requests = live_server
        client = TusClient(base_url, add_request_id=True, headers={"X-Request-ID": "fixed-id"})
        client.upload_file(payload_file)
        ids = {_header(h, "X-Request-ID") for _, _, h in requests}
        assert ids == {"fixed-id"}


class TestOnUploadUrlAvailable:
    def test_fires_on_creation(self, live_server, payload_file):
        base_url, _, _ = live_server
        seen = []
        client = TusClient(base_url, on_upload_url_available=seen.append)
        url = client.upload_file(payload_file)
        assert seen == [url]

    def test_fires_on_resume_from_url_storage(self, live_server, payload_file):
        from resumable_upload.url_storage import InMemoryURLStorage

        base_url, _, _ = live_server
        seen = []
        url_storage = InMemoryURLStorage()
        client = TusClient(
            base_url,
            store_url=True,
            url_storage=url_storage,
            on_upload_url_available=seen.append,
        )
        first = client.upload_file(payload_file)
        # Second call resolves the URL from storage (resume path).
        second = client.upload_file(payload_file)
        assert first == second
        assert seen == [first, first]


class TestMetadataForPartialUploads:
    def test_partials_carry_metadata(self, live_server, payload_file):
        base_url, storage, requests = live_server
        client = TusClient(base_url)
        url = client.upload_file(
            payload_file,
            parallel_uploads=2,
            metadata_for_partial_uploads={"purpose": "slice"},
        )
        upload_id = url.rsplit("/", 1)[1]
        assert storage.read_file(upload_id) == b"hello ergonomic world"

        partial_creates = [
            h
            for m, _, h in requests
            if m == "POST" and (_header(h, "Upload-Concat") or "") == "partial"
        ]
        assert len(partial_creates) == 2
        for h in partial_creates:
            meta = _header(h, "Upload-Metadata") or ""
            assert "purpose" in meta

    def test_partials_have_no_metadata_by_default(self, live_server, payload_file):
        base_url, _, requests = live_server
        TusClient(base_url).upload_file(payload_file, parallel_uploads=2)
        partial_creates = [
            h
            for m, _, h in requests
            if m == "POST" and (_header(h, "Upload-Concat") or "") == "partial"
        ]
        assert len(partial_creates) == 2
        assert all(not _header(h, "Upload-Metadata") for h in partial_creates)


class TestAsyncErgonomics:
    @pytest.mark.anyio
    async def test_async_override_and_request_id(self, live_server, payload_file):
        pytest.importorskip("httpx")
        from resumable_upload import AsyncTusClient

        base_url, storage, requests = live_server
        seen = []
        client = AsyncTusClient(
            base_url,
            override_patch_method=True,
            add_request_id=True,
            on_upload_url_available=seen.append,
        )
        url = await client.upload_file(payload_file)

        upload_id = url.rsplit("/", 1)[1]
        assert storage.read_file(upload_id) == b"hello ergonomic world"
        assert seen == [url]

        patches = [h for m, _, h in requests if m == "PATCH"]
        assert patches
        assert all(_header(h, "X-HTTP-Method-Override") == "PATCH" for h in patches)
        ids = [_header(h, "X-Request-ID") for _, _, h in requests]
        assert all(ids) and len(set(ids)) == len(ids)
