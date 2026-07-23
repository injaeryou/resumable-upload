"""Regression tests for the 2026-07 branch review findings.

Each test class references the finding it locks down; see
.docs/research/2026-07-16-branch-review-findings.md for the full report.
"""

import base64
import os
import shutil
import socket
import tempfile
import threading
import urllib.request
from http.server import HTTPServer

import pytest

from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def storage():
    temp_dir = tempfile.mkdtemp()
    try:
        yield SQLiteStorage(
            db_path=os.path.join(temp_dir, "u.db"),
            upload_dir=os.path.join(temp_dir, "files"),
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _h(**extra):
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


def _create_partial(server, length):
    _, headers, _ = server.handle_request(
        "POST",
        "/files",
        _h(**{"Upload-Length": str(length), "Upload-Concat": "partial"}),
        b"",
    )
    return headers["Location"]


def _patch(server, location, offset, data):
    return server.handle_request(
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": str(offset),
                "Content-Type": "application/offset+octet-stream",
            }
        ),
        data,
    )


class TestChunkedParserHardening:
    """Finding 0: negative chunk size read-to-EOF + caps applied only after the loop."""

    @pytest.fixture
    def live(self):
        temp_dir = tempfile.mkdtemp()
        storage = SQLiteStorage(
            db_path=os.path.join(temp_dir, "u.db"),
            upload_dir=os.path.join(temp_dir, "files"),
        )
        tus = TusServer(
            storage=storage,
            base_path="/files",
            supports_checksum_trailer=True,
            max_chunk_size=1024,
        )

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = tus
        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield port
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _create(self, port, length):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/files",
            method="POST",
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(length)},
        )
        with urllib.request.urlopen(req) as resp:
            return resp.headers["Location"]

    def _raw_status(self, port, payload: bytes) -> int:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(payload)
            sock.settimeout(5)
            data = b""
            while b"\r\n" not in data:
                got = sock.recv(65536)
                if not got:
                    break
                data += got
        return int(data.split(b"\r\n", 1)[0].split(b" ", 2)[1])

    def _chunked_patch_head(self, location: str) -> str:
        return (
            f"PATCH {location} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Tus-Resumable: 1.0.0\r\n"
            "Upload-Offset: 0\r\n"
            "Content-Type: application/offset+octet-stream\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n"
            "\r\n"
        )

    def test_negative_chunk_size_rejected_immediately(self, live):
        port = live
        location = self._create(port, 10)
        payload = self._chunked_patch_head(location).encode() + b"-1\r\nxxxxx\r\n0\r\n\r\n"
        assert self._raw_status(port, payload) == 400

    def test_declared_chunk_exceeding_cap_rejected_before_read(self, live):
        # A single declared 1 MiB chunk against max_chunk_size=1024 must be
        # rejected from the size line alone — no need to consume the body.
        port = live
        location = self._create(port, 4096)
        payload = self._chunked_patch_head(location).encode() + b"100000\r\n"
        assert self._raw_status(port, payload) == 413


class TestAssemblyRobustness:
    """Findings 1+3+8: CAS rollback on copy failure, max_size at assembly,
    atomic pending-final row, HEAD retriggers a stranded assembly."""

    def test_copy_failure_rolls_back_claim_and_head_retries(self, storage, monkeypatch):
        server = TusServer(storage=storage, base_path="/files")
        p = _create_partial(server, 5)
        _, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Concat": f"final;{p}"}), b""
        )
        final_loc = headers["Location"]

        # Make the byte copy fail once at assembly time.
        real_copy = SQLiteStorage._copy_partials
        calls = {"n": 0}

        def flaky(self, final_id, partials):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_copy(self, final_id, partials)

        monkeypatch.setattr(SQLiteStorage, "_copy_partials", flaky)

        # Completing the partial triggers assembly; the copy fails but the
        # PATCH must still succeed and the final must stay pending (claim
        # rolled back), not stranded.
        status, _, _ = _patch(server, p, 0, b"hello")
        assert status == 204

        # HEAD on the pending final retries the assembly and succeeds.
        status, head_headers, _ = server.handle_request("HEAD", final_loc, _h(), b"")
        assert status == 200
        assert head_headers["Upload-Length"] == "5"
        assert head_headers["Upload-Offset"] == "5"
        assert calls["n"] == 2

    def test_final_over_deferred_partials_rejected_when_max_size_set(self, storage):
        # Deferred-length partials make the final's size unknowable at
        # create time, and a later assembly-time refusal could only destroy
        # the final silently (no client request left to answer 413 to).
        # With Tus-Max-Size configured, the final must be rejected up front.
        server = TusServer(storage=storage, base_path="/files", max_size=8)
        locs = []
        for _ in range(2):
            _, h, _ = server.handle_request(
                "POST",
                "/files",
                _h(**{"Upload-Defer-Length": "1", "Upload-Concat": "partial"}),
                b"",
            )
            locs.append(h["Location"])

        status, _, body = server.handle_request(
            "POST", "/files", _h(**{"Upload-Concat": "final;" + " ".join(locs)}), b""
        )
        assert status == 400
        assert b"deferred-length" in body

    def test_final_over_deferred_partials_allowed_without_max_size(self, storage):
        # No Tus-Max-Size — nothing to enforce, the pending final is fine.
        server = TusServer(storage=storage, base_path="/files")
        _, h, _ = server.handle_request(
            "POST",
            "/files",
            _h(**{"Upload-Defer-Length": "1", "Upload-Concat": "partial"}),
            b"",
        )
        status, _, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Concat": "final;" + h["Location"]}), b""
        )
        assert status == 201

    @pytest.mark.anyio
    async def test_final_over_deferred_partials_rejected_async(self, storage, anyio_backend):
        # Async parity: handle_create_final_async must reject the same request
        # the sync path does (previously it silently accepted a pending final).
        server = TusServer(storage=storage, base_path="/files", max_size=8)
        locs = []
        for _ in range(2):
            _, h, _ = await server.handle_request_async(
                "POST",
                "/files",
                _h(**{"Upload-Defer-Length": "1", "Upload-Concat": "partial"}),
                b"",
            )
            locs.append(h["Location"])

        status, _, body = await server.handle_request_async(
            "POST", "/files", _h(**{"Upload-Concat": "final;" + " ".join(locs)}), b""
        )
        assert status == 400
        assert b"deferred-length" in body

    def test_transport_level_error_carries_cors(self, storage):
        # A chunked PATCH whose declared chunk exceeds max_chunk_size is
        # rejected by the transport parser (_send_error), short-circuiting the
        # core. That 413 must still carry CORS so a browser can read the status.
        server = TusServer(
            storage=storage,
            base_path="/files",
            cors_allow_origins="*",
            max_chunk_size=10,
        )

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = server
        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=5)
            req = (
                "PATCH /files/00000000-0000-0000-0000-000000000000 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Tus-Resumable: 1.0.0\r\n"
                "Upload-Offset: 0\r\n"
                "Content-Type: application/offset+octet-stream\r\n"
                "Origin: https://app.example\r\n"
                "Transfer-Encoding: chunked\r\n"
                "\r\n"
                "14\r\n"  # 0x14 = 20 bytes, over the 10-byte max_chunk_size
                "xxxxxxxxxxxxxxxxxxxx\r\n"
                "0\r\n\r\n"
            )
            conn.sendall(req.encode())
            raw = b""
            while b"\r\n\r\n" not in raw:
                part = conn.recv(4096)
                if not part:
                    break
                raw += part
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

        resp = raw.decode("latin1")
        assert "413" in resp.split("\r\n", 1)[0]
        assert "Access-Control-Allow-Origin: *" in resp

    def test_pending_final_row_is_atomic(self, storage):
        # Finding 8: the row must carry concat_partial_ids from birth — a
        # crash between INSERT and UPDATE previously left a plain orphan row.
        server = TusServer(storage=storage, base_path="/files")
        p = _create_partial(server, 5)
        _, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Concat": f"final;{p}"}), b""
        )
        final_id = headers["Location"].rsplit("/", 1)[1]
        row = storage.get_upload(final_id)
        assert row["concat_partial_ids"] is not None


class TestPendingFinalHead:
    """Finding 2: HEAD on a pending final must not claim Upload-Offset: 0."""

    def test_no_offset_until_assembled(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        p = _create_partial(server, 5)
        _, headers, _ = server.handle_request(
            "POST", "/files", _h(**{"Upload-Concat": f"final;{p}"}), b""
        )
        status, head_headers, _ = server.handle_request("HEAD", headers["Location"], _h(), b"")
        assert status == 200
        assert "Upload-Offset" not in head_headers
        assert "Upload-Length" not in head_headers
        assert head_headers["Upload-Concat"].startswith("final;")


class TestDownloadFilenameEncoding:
    """Finding 4: non-ASCII filenames must not break header emission."""

    def test_non_ascii_filename_uses_rfc5987(self, storage):
        server = TusServer(storage=storage, base_path="/files", enable_downloads=True)
        meta = "filename " + base64.b64encode("보고서.docx".encode()).decode()
        _, headers, _ = server.handle_request(
            "POST",
            "/files",
            _h(
                **{
                    "Upload-Length": "2",
                    "Upload-Metadata": meta,
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"hi",
        )
        status, get_headers, body = server.handle_request("GET", headers["Location"], _h(), b"")
        assert status == 200
        disposition = get_headers["Content-Disposition"]
        # Header value must be latin-1 encodable (stdlib transport requirement)
        disposition.encode("latin-1")
        assert "filename*=UTF-8''" in disposition

    def test_ascii_filename_unchanged(self, storage):
        server = TusServer(storage=storage, base_path="/files", enable_downloads=True)
        meta = "filename " + base64.b64encode(b"report.docx").decode()
        _, headers, _ = server.handle_request(
            "POST",
            "/files",
            _h(
                **{
                    "Upload-Length": "2",
                    "Upload-Metadata": meta,
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"hi",
        )
        _, get_headers, _ = server.handle_request("GET", headers["Location"], _h(), b"")
        assert 'filename="report.docx"' in get_headers["Content-Disposition"]


class TestCreationWithUploadPartialHook:
    """Finding 5: partial completed via creation-with-upload must not fire
    on_upload_complete (parity with the PATCH path)."""

    def test_hook_suppressed(self, storage):
        calls = []
        server = TusServer(
            storage=storage,
            base_path="/files",
            on_upload_complete=lambda uid, m, i: calls.append(uid),
        )
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _h(
                **{
                    "Upload-Length": "2",
                    "Upload-Concat": "partial",
                    "Content-Type": "application/offset+octet-stream",
                }
            ),
            b"hi",
        )
        assert status == 201
        assert calls == []


class TestCallbackMethodConsistency:
    """Finding 9: before/after request callbacks must report the same method
    when override_patch_method tunnels PATCH through POST."""

    def test_after_response_reports_tunneled_method(self, storage, tmp_path):
        from resumable_upload import TusClient

        tus = TusServer(storage=storage, base_path="/files")

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = tus
        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            payload = tmp_path / "f.bin"
            payload.write_bytes(b"hello")
            seen = []
            client = TusClient(
                f"http://127.0.0.1:{port}/files",
                override_patch_method=True,
                before_request=lambda m, u, h: seen.append(("before", m)),
                after_response=lambda m, u, s: seen.append(("after", m)),
            )
            client.upload_file(str(payload))
            data_methods = {m for phase, m in seen if m not in ("HEAD",)}
            befores = [m for phase, m in seen if phase == "before"]
            afters = [m for phase, m in seen if phase == "after"]
            assert befores == afters, "each request must report one consistent method"
            assert "PATCH" not in data_methods
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestAsyncParity:
    """Finding 7: async PATCH paths must not call sync storage methods."""

    @pytest.mark.anyio
    async def test_async_chunk_stop_uses_async_delete(self, storage):
        from resumable_upload.exceptions import TusHookError

        deleted = []
        real = type(storage).delete_upload_async

        async def spy(self, upload_id):
            deleted.append(upload_id)
            await real(self, upload_id)

        type(storage).delete_upload_async = spy
        try:

            def hook(uid, offset, length):
                raise TusHookError("stop", status_code=429)

            server = TusServer(storage=storage, base_path="/files", on_chunk_received=hook)
            _, headers, _ = server.handle_request(
                "POST", "/files", _h(**{"Upload-Length": "5"}), b""
            )
            status, _, _ = await server.handle_request_async(
                "PATCH",
                headers["Location"],
                _h(
                    **{
                        "Upload-Offset": "0",
                        "Content-Type": "application/offset+octet-stream",
                    }
                ),
                b"hello",
            )
            assert status == 429
            assert len(deleted) == 1
        finally:
            type(storage).delete_upload_async = real
