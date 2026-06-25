# tests/test_async_client.py
from __future__ import annotations

import pytest

from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

httpx = pytest.importorskip("httpx")


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
        storage=SQLiteStorage(
            db_path=str(tmp_path / "u.db"), upload_dir=str(tmp_path / "files")
        ),
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
