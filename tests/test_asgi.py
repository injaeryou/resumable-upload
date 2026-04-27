"""ASGI adapter roundtrip tests using httpx.ASGITransport."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

httpx = pytest.importorskip("httpx")

from resumable_upload.asgi import TusASGIApp  # noqa: E402
from resumable_upload.metrics import MetricsRegistry  # noqa: E402
from resumable_upload.server import TusServer  # noqa: E402
from resumable_upload.storage import SQLiteStorage  # noqa: E402


@pytest.fixture
def app():
    temp_dir = tempfile.mkdtemp()
    try:
        storage = SQLiteStorage(
            db_path=os.path.join(temp_dir, "u.db"),
            upload_dir=os.path.join(temp_dir, "files"),
        )
        server = TusServer(storage=storage, base_path="/files")
        yield TusASGIApp(server)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def app_with_metrics():
    temp_dir = tempfile.mkdtemp()
    try:
        storage = SQLiteStorage(
            db_path=os.path.join(temp_dir, "u.db"),
            upload_dir=os.path.join(temp_dir, "files"),
        )
        server = TusServer(
            storage=storage,
            base_path="/files",
            metrics_registry=MetricsRegistry(),
        )
        yield TusASGIApp(server)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.anyio
async def test_asgi_options(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.options("/files")
        assert r.status_code == 204
        assert "Tus-Extension" in r.headers
        assert "concatenation" in r.headers["Tus-Extension"]


@pytest.mark.anyio
async def test_asgi_create_and_patch_roundtrip(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/files",
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": "5"},
        )
        assert r.status_code == 201
        location = r.headers["Location"]
        r = await c.patch(
            location,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            content=b"hello",
        )
        assert r.status_code == 204
        assert r.headers["Upload-Offset"] == "5"


@pytest.mark.anyio
async def test_asgi_head_returns_offset(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/files", headers={"Tus-Resumable": "1.0.0", "Upload-Length": "10"})
        location = r.headers["Location"]
        r = await c.head(location, headers={"Tus-Resumable": "1.0.0"})
        assert r.status_code == 200
        assert r.headers["Upload-Length"] == "10"
        assert r.headers["Upload-Offset"] == "0"


@pytest.mark.anyio
async def test_asgi_404_on_unknown_upload(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.head(
            "/files/00000000-0000-0000-0000-000000000000",
            headers={"Tus-Resumable": "1.0.0"},
        )
        assert r.status_code == 404


@pytest.mark.anyio
async def test_asgi_metrics_disabled_by_default(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/metrics")
        assert r.status_code == 404


@pytest.mark.anyio
async def test_asgi_metrics_exposed_when_registry_attached(app_with_metrics):
    transport = httpx.ASGITransport(app=app_with_metrics)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.options("/files")
        r = await c.get("/metrics")
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith("text/plain")
        assert "tusd_requests_total" in r.text


@pytest.fixture
def anyio_backend():
    return "asyncio"
