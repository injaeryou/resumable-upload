"""Prove a native-async storage backend is awaited end to end.

Loads ``AsyncDictStorage`` from ``examples/server/async_storage.py`` (the
true-async backend template) and asserts that ``handle_request_async`` drives
its native ``*_async`` overrides — never the ``asyncio.to_thread`` fallback
that the Storage ABC provides for sync-only backends.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest

from resumable_upload.server import TusServer

_EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "server" / "async_storage.py"


def _load_backend_cls():
    spec = importlib.util.spec_from_file_location("async_storage_example", _EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.AsyncDictStorage


AsyncDictStorage = _load_backend_cls()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _h(**extra: str) -> dict[str, str]:
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


@pytest.fixture
def server() -> TusServer:
    # lock_backend=None: this test asserts the STORAGE async path is native
    # (no to_thread). The default InMemoryLockBackend legitimately wraps its
    # sync acquire/release in to_thread, which is unrelated to storage I/O.
    return TusServer(storage=AsyncDictStorage(), base_path="/files", lock_backend=None)


@pytest.mark.anyio
async def test_native_async_methods_roundtrip() -> None:
    """The backend's own async API works without ever calling its sync API."""
    storage = AsyncDictStorage()
    await storage.create_upload_async("u1", 5, {"k": "v"})
    rec = await storage.get_upload_async("u1")
    assert rec is not None
    assert rec["offset"] == 0
    assert rec["upload_length"] == 5

    await storage.write_chunk_async("u1", 0, b"hello")
    assert await storage.update_offset_atomic_async("u1", 0, 5) is True
    # Stale expected offset must be rejected, like every backend.
    assert await storage.update_offset_atomic_async("u1", 0, 9) is False
    assert await storage.complete_upload_async("u1") is True
    assert await storage.complete_upload_async("u1") is False  # idempotent
    assert await storage.read_file_async("u1") == b"hello"


@pytest.mark.anyio
async def test_async_dispatch_never_falls_back_to_to_thread(
    server: TusServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full upload via handle_request_async must touch zero to_thread calls.

    If any storage I/O fell through to the Storage ABC default (which wraps
    the sync method in ``asyncio.to_thread``), this boom fires. Success here
    means every awaited I/O hit a native ``*_async`` override.
    """

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("to_thread fallback used — backend async path is not native")

    monkeypatch.setattr(asyncio, "to_thread", _boom)

    payload = b"hello-async-world"
    digest = base64.b64encode(hashlib.sha1(payload).digest()).decode()  # noqa: S324

    # POST create
    status, headers, _ = await server.handle_request_async(
        "POST", "/files", _h(**{"Upload-Length": str(len(payload))}), b""
    )
    assert status == 201
    location = headers["Location"]

    # PATCH the whole body with a checksum (exercises the validation path too)
    status, headers, _ = await server.handle_request_async(
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Checksum": f"sha1 {digest}",
            }
        ),
        payload,
    )
    assert status == 204
    assert headers["Upload-Offset"] == str(len(payload))

    # HEAD reports completion offset
    status, headers, _ = await server.handle_request_async("HEAD", location, _h(), b"")
    assert status == 200
    assert headers["Upload-Offset"] == str(len(payload))

    # Integrity straight from the backend's async read path
    uid = location.rsplit("/", 1)[-1]
    assert await server.storage.read_file_async(uid) == payload

    # DELETE then HEAD 404
    status, _, _ = await server.handle_request_async("DELETE", location, _h(), b"")
    assert status == 204
    status, _, _ = await server.handle_request_async("HEAD", location, _h(), b"")
    assert status == 404


def test_sync_dispatch_still_works_on_the_same_backend(server: TusServer) -> None:
    """The backend remains a valid sync Storage too (no async required)."""
    status, headers, _ = server.handle_request("POST", "/files", _h(**{"Upload-Length": "3"}), b"")
    assert status == 201
    location = headers["Location"]
    status, headers, _ = server.handle_request(
        "PATCH",
        location,
        _h(**{"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"}),
        b"abc",
    )
    assert status == 204
    assert headers["Upload-Offset"] == "3"
