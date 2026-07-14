"""Parametric sync/async dispatch tests.

Verifies that ``TusServerCore.handle_request_async`` produces identical
results to ``TusServerCore.handle_request`` for every representative
scenario in ``test_server.py``. ``SQLiteStorage`` is used so the async
path falls back to ``asyncio.to_thread`` wrappers — the contract under
test is exactly the equivalence of the two dispatch paths.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(tmp_path):
    return TusServer(
        storage=SQLiteStorage(
            db_path=str(tmp_path / "uploads.db"),
            upload_dir=str(tmp_path / "files"),
        ),
        base_path="/files",
        checksum_algorithms=("sha1", "sha256"),
    )


def _h(**extra: str) -> dict[str, str]:
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


def _sync_dispatch(
    server: TusServer, method: str, path: str, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    return server.handle_request(method, path, headers, body)


def _async_dispatch(
    server: TusServer, method: str, path: str, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    return asyncio.run(server.handle_request_async(method, path, headers, body))


@pytest.fixture(params=[_sync_dispatch, _async_dispatch], ids=["sync", "async"])
def dispatch(request) -> Any:
    return request.param


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_options_capabilities(server, dispatch):
    status, headers, _ = dispatch(server, "OPTIONS", "/files", {}, b"")
    assert status == 204
    assert headers["Tus-Resumable"] == "1.0.0"
    assert "creation" in headers["Tus-Extension"]


def test_post_creates_upload(server, dispatch):
    status, headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Length": "5"}), b"")
    assert status == 201
    assert headers["Location"].startswith("/files/")


def test_post_with_initial_body(server, dispatch):
    status, headers, _ = dispatch(
        server,
        "POST",
        "/files",
        _h(**{"Upload-Length": "5", "Content-Type": "application/offset+octet-stream"}),
        b"hello",
    )
    assert status == 201
    assert headers["Upload-Offset"] == "5"


def test_post_missing_length(server, dispatch):
    status, _, _ = dispatch(server, "POST", "/files", _h(), b"")
    assert status == 400


def test_head_offset_progress(server, dispatch):
    _, post_headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Length": "10"}), b"")
    location = post_headers["Location"]
    status, headers, _ = dispatch(server, "HEAD", location, _h(), b"")
    assert status == 200
    assert headers["Upload-Offset"] == "0"
    assert headers["Upload-Length"] == "10"


def test_head_404_for_unknown(server, dispatch):
    bogus = "/files/00000000-0000-0000-0000-000000000000"
    status, _, _ = dispatch(server, "HEAD", bogus, _h(), b"")
    assert status == 404


def test_patch_appends_chunk(server, dispatch):
    _, post_headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Length": "5"}), b"")
    location = post_headers["Location"]
    status, headers, _ = dispatch(
        server,
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        b"hello",
    )
    assert status == 204
    assert headers["Upload-Offset"] == "5"


def test_patch_with_sha256_accepted(server, dispatch):
    _, post_headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Length": "5"}), b"")
    location = post_headers["Location"]
    digest = base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    status, _, _ = dispatch(
        server,
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Checksum": f"sha256 {digest}",
            }
        ),
        b"hello",
    )
    assert status == 204


def test_patch_offset_mismatch_returns_409(server, dispatch):
    _, post_headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Length": "10"}), b"")
    location = post_headers["Location"]
    status, _, _ = dispatch(
        server,
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": "5",  # stale: actual offset is 0
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        b"x",
    )
    assert status == 409


def test_delete_removes_upload(server, dispatch):
    _, post_headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Length": "3"}), b"")
    location = post_headers["Location"]
    status, _, _ = dispatch(server, "DELETE", location, _h(), b"")
    assert status == 204
    status, _, _ = dispatch(server, "HEAD", location, _h(), b"")
    assert status == 404


def test_defer_length_commits_on_first_patch(server, dispatch):
    _, post_headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Defer-Length": "1"}), b"")
    location = post_headers["Location"]
    status, headers, _ = dispatch(
        server,
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": "0",
                "Upload-Length": "3",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        b"abc",
    )
    assert status == 204
    assert headers["Upload-Offset"] == "3"


def test_concatenation_final_merges_partials(server, dispatch):
    # Create two partial uploads
    p1_status, p1_headers, _ = dispatch(
        server,
        "POST",
        "/files",
        _h(**{"Upload-Length": "5", "Upload-Concat": "partial"}),
        b"",
    )
    p1_loc = p1_headers["Location"]
    dispatch(
        server,
        "PATCH",
        p1_loc,
        _h(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        b"hello",
    )

    p2_status, p2_headers, _ = dispatch(
        server,
        "POST",
        "/files",
        _h(**{"Upload-Length": "6", "Upload-Concat": "partial"}),
        b"",
    )
    p2_loc = p2_headers["Location"]
    dispatch(
        server,
        "PATCH",
        p2_loc,
        _h(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        b"-world",
    )

    # Concat-final
    status, headers, _ = dispatch(
        server,
        "POST",
        "/files",
        _h(**{"Upload-Concat": f"final;{p1_loc} {p2_loc}"}),
        b"",
    )
    assert status == 201
    assert headers["Upload-Length"] == "11"


def test_head_final_echoes_upload_concat(server, dispatch):
    # HEAD on a final upload must reconstruct `Upload-Concat: final;<urls>`.
    locations = []
    for data in (b"hello", b"-world"):
        _, headers, _ = dispatch(
            server,
            "POST",
            "/files",
            _h(**{"Upload-Length": str(len(data)), "Upload-Concat": "partial"}),
            b"",
        )
        loc = headers["Location"]
        dispatch(
            server,
            "PATCH",
            loc,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            data,
        )
        locations.append(loc)

    status, headers, _ = dispatch(
        server,
        "POST",
        "/files",
        _h(**{"Upload-Concat": f"final;{locations[0]} {locations[1]}"}),
        b"",
    )
    assert status == 201

    status, head_headers, _ = dispatch(server, "HEAD", headers["Location"], _h(), b"")
    assert status == 200
    assert head_headers["Upload-Concat"] == f"final;{locations[0]} {locations[1]}"


def test_head_partial_advertises_upload_concat(server, dispatch):
    # Concatenation extension: HEAD on a partial upload must echo Upload-Concat.
    _, post_headers, _ = dispatch(
        server,
        "POST",
        "/files",
        _h(**{"Upload-Length": "5", "Upload-Concat": "partial"}),
        b"",
    )
    location = post_headers["Location"]
    status, headers, _ = dispatch(server, "HEAD", location, _h(), b"")
    assert status == 200
    assert headers.get("Upload-Concat") == "partial"


def test_head_non_partial_omits_upload_concat(server, dispatch):
    _, post_headers, _ = dispatch(server, "POST", "/files", _h(**{"Upload-Length": "5"}), b"")
    location = post_headers["Location"]
    status, headers, _ = dispatch(server, "HEAD", location, _h(), b"")
    assert status == 200
    assert "Upload-Concat" not in headers


def test_async_cleanup_no_deadlock_with_zero_interval(tmp_path):
    """Regression: concurrent async requests must not deadlock during cleanup.

    With ``cleanup_interval <= 0`` the double-checked guard always passes, so
    the old ``with self._cleanup_lock:`` (a threading.Lock held across an
    ``await``) let a second coroutine block the event loop forever. The
    non-blocking ``_cleanup_running`` flag must keep this lock-free.

    Run the loop in a worker thread and join with a timeout: a true deadlock
    freezes the loop thread, so an in-loop ``asyncio.wait_for`` could never
    fire — only an outside thread can observe the hang.
    """
    import threading

    srv = TusServer(
        storage=SQLiteStorage(db_path=str(tmp_path / "u.db"), upload_dir=str(tmp_path / "f")),
        base_path="/files",
        upload_expiry=3600,
        cleanup_interval=0,
    )

    results: list[tuple[int, dict[str, str], bytes]] = []

    def run() -> None:
        async def both() -> None:
            # Two concurrent OPTIONS; each triggers end-of-dispatch cleanup.
            results.extend(
                await asyncio.gather(
                    srv.handle_request_async("OPTIONS", "/files", _h(), b""),
                    srv.handle_request_async("OPTIONS", "/files", _h(), b""),
                )
            )

        asyncio.run(both())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "async cleanup deadlocked (threading.Lock held across await)"
    assert all(status == 204 for status, _, _ in results)
