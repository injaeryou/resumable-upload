# tests/test_async_client.py
from __future__ import annotations

import pytest

from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

httpx = pytest.importorskip("httpx")


def test_async_symbols_lazily_exported():
    import resumable_upload

    assert "AsyncTusClient" in resumable_upload.__all__
    from resumable_upload import AsyncTusClient, AsyncUploader  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def asgi_base(tmp_path):
    """A TusASGIApp + an httpx AsyncClient wired to it via ASGITransport.

    No sockets: requests are dispatched in-process straight into
    handle_request_async, so this also exercises the real async server path.
    """
    tus = TusServer(
        storage=SQLiteStorage(db_path=str(tmp_path / "u.db"), upload_dir=str(tmp_path / "files")),
        base_path="/files",
    )
    app = TusASGIApp(tus)
    transport = httpx.ASGITransport(app=app)
    return transport, "http://testserver/files"


@pytest.mark.anyio
async def test_http_request_helper_returns_response(asgi_base):
    from resumable_upload.client.aio import _http

    transport, base = asgi_base
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await _http.request(c, "OPTIONS", base, headers={})
        assert resp.status_code == 204


@pytest.mark.anyio
async def test_async_uploader_uploads_in_chunks(asgi_base):
    import io
    import os

    from resumable_upload.client.aio.uploader import AsyncUploader

    transport, base = asgi_base
    payload = os.urandom(50_000)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        # create an upload via raw POST
        r = await c.request(
            "POST", base, headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(len(payload))}
        )
        url = r.headers["Location"]
        up = await AsyncUploader.open(c, url, file_stream=io.BytesIO(payload), chunk_size=16_384)
        await up.upload()
        assert up.is_complete
        # verify server offset == length
        head = await c.request("HEAD", url, headers={"Tus-Resumable": "1.0.0"})
        assert head.headers["Upload-Offset"] == str(len(payload))


@pytest.mark.anyio
async def test_async_uploader_stop_event_cancels_without_retries(asgi_base):
    """stop_event must cancel even when chunks succeed and retries are off."""
    import asyncio
    import io
    import os

    from resumable_upload.client.aio.uploader import AsyncUploader
    from resumable_upload.exceptions import TusUploadFailed

    transport, base = asgi_base
    payload = os.urandom(50_000)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.request(
            "POST", base, headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(len(payload))}
        )
        url = r.headers["Location"]
        stop = asyncio.Event()
        stop.set()
        up = await AsyncUploader.open(
            c, url, file_stream=io.BytesIO(payload), chunk_size=16_384, stop_event=stop
        )
        with pytest.raises(TusUploadFailed, match="cancelled via stop_event"):
            await up.upload()


@pytest.mark.anyio
async def test_async_uploader_checksum_roundtrip(asgi_base):
    import io

    from resumable_upload.client.aio.uploader import AsyncUploader

    transport, base = asgi_base
    data = b"hello async world"
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.request(
            "POST",
            base,
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(len(data))},
        )
        url = r.headers["Location"]
        up = await AsyncUploader.open(
            c, url, file_stream=io.BytesIO(data), checksum="sha1", chunk_size=4
        )
        await up.upload()
        assert up.is_complete


@pytest.mark.anyio
async def test_async_client_upload_file_roundtrip(asgi_base, tmp_path):
    import os

    from resumable_upload.client.aio.client import AsyncTusClient

    transport, base = asgi_base
    f = tmp_path / "data.bin"
    payload = os.urandom(40_000)
    f.write_bytes(payload)
    async with AsyncTusClient(base, _transport=transport, chunk_size=8192) as client:
        url = await client.upload_file(str(f))
        info = await client.get_upload_info(url)
        assert info["complete"] is True
        assert info["offset"] == len(payload)
        await client.delete_upload(url)
        # second delete tolerated (404)
        await client.delete_upload(url)


@pytest.mark.anyio
async def test_async_client_server_info_and_metadata(asgi_base, tmp_path):
    from resumable_upload.client.aio.client import AsyncTusClient

    transport, base = asgi_base
    f = tmp_path / "m.bin"
    f.write_bytes(b"xyz")
    async with AsyncTusClient(base, _transport=transport) as client:
        info = await client.get_server_info()
        assert info["version"] == "1.0.0"
        assert "creation" in info["extensions"]
        url = await client.upload_file(str(f), metadata={"filename": "m.bin"})
        md = await client.get_metadata(url)
        assert md["filename"] == "m.bin"


@pytest.mark.anyio
async def test_async_concatenation_merges_partials(asgi_base, tmp_path):
    from resumable_upload.client.aio.client import AsyncTusClient

    transport, base = asgi_base
    a = tmp_path / "a"
    a.write_bytes(b"hello")
    b = tmp_path / "b"
    b.write_bytes(b"-world")
    async with AsyncTusClient(base, _transport=transport) as client:
        p1 = await client.create_partial_upload(str(a))
        p2 = await client.create_partial_upload(str(b))
        final = await client.create_final_upload([p1, p2], metadata={"filename": "hw.bin"})
        info = await client.get_upload_info(final)
        assert info["length"] == 11


@pytest.mark.anyio
async def test_create_partial_upload_requires_source(asgi_base):
    from resumable_upload.client.aio.client import AsyncTusClient

    transport, base = asgi_base
    async with AsyncTusClient(base, _transport=transport) as client:
        with pytest.raises(ValueError, match="file_path or file_stream"):
            await client.create_partial_upload()


@pytest.mark.anyio
async def test_create_final_upload_requires_urls(asgi_base):
    from resumable_upload.client.aio.client import AsyncTusClient

    transport, base = asgi_base
    async with AsyncTusClient(base, _transport=transport) as client:
        with pytest.raises(ValueError, match="at least one"):
            await client.create_final_upload([])


@pytest.mark.anyio
async def test_async_parallel_upload_merges(asgi_base, tmp_path):
    import os

    from resumable_upload.client.aio.client import AsyncTusClient

    transport, base = asgi_base
    f = tmp_path / "big.bin"
    payload = os.urandom(100_000)
    f.write_bytes(payload)
    async with AsyncTusClient(base, _transport=transport, chunk_size=8192) as client:
        url = await client.upload_file(str(f), parallel_uploads=4)
        info = await client.get_upload_info(url)
        assert info["length"] == len(payload)
        assert info["complete"] is True
