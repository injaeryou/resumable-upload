"""Tests for multi-algorithm checksum support."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import tempfile

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage

# Every algorithm the checksum registry ships. The server fixture enables all
# of them so each one is exercised end-to-end (registry entries with no test
# are how sha512 silently rotted before).
ALL_ALGORITHMS = ("sha1", "sha256", "sha512", "md5")


@pytest.fixture
def server():
    temp_dir = tempfile.mkdtemp()
    try:
        yield TusServer(
            storage=SQLiteStorage(
                db_path=os.path.join(temp_dir, "u.db"),
                upload_dir=os.path.join(temp_dir, "files"),
            ),
            base_path="/files",
            checksum_algorithms=ALL_ALGORITHMS,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _h(**extra):
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


def _dispatch(server, mode: str, method: str, path: str, headers: dict, body: bytes):
    """Route a request through the sync or async dispatch path.

    Both share ``_plan_patch`` for checksum validation, so running the same
    matrix through each proves the single-sourced logic holds on both.
    """
    if mode == "async":
        return asyncio.run(server.handle_request_async(method, path, headers, body))
    return server.handle_request(method, path, headers, body)


def _create_upload(server, length: int) -> str:
    _, headers, _ = server.handle_request(
        "POST", "/files", _h(**{"Upload-Length": str(length)}), b""
    )
    return headers["Location"]


def _checksum_patch(server, mode, location, algo, body, *, digest_src=None):
    """PATCH ``body`` with an ``Upload-Checksum`` for ``algo``.

    ``digest_src`` defaults to ``body``; pass different bytes to forge a
    mismatching checksum.
    """
    src = body if digest_src is None else digest_src
    digest = base64.b64encode(hashlib.new(algo, src).digest()).decode()
    return _dispatch(
        server,
        mode,
        "PATCH",
        location,
        _h(
            **{
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Checksum": f"{algo} {digest}",
            }
        ),
        body,
    )


class TestChecksumAlgorithms:
    def test_options_advertises_all_algorithms(self, server):
        status, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert status == 204
        algos = set(headers["Tus-Checksum-Algorithm"].split(","))
        assert set(ALL_ALGORITHMS) <= algos

    @pytest.mark.parametrize("mode", ["sync", "async"])
    @pytest.mark.parametrize("algo", ALL_ALGORITHMS)
    def test_patch_with_matching_checksum_accepted(self, server, algo, mode):
        location = _create_upload(server, 5)
        status, headers, _ = _checksum_patch(server, mode, location, algo, b"hello")
        assert status == 204
        assert headers["Upload-Offset"] == "5"

    @pytest.mark.parametrize("mode", ["sync", "async"])
    @pytest.mark.parametrize("algo", ALL_ALGORITHMS)
    def test_patch_with_mismatching_checksum_rejected(self, server, algo, mode):
        location = _create_upload(server, 5)
        # Digest computed over different bytes than the body -> 460.
        status, _, _ = _checksum_patch(server, mode, location, algo, b"hello", digest_src=b"WRONG")
        assert status == 460

    def test_unsupported_algorithm_rejected(self, server):
        location = _create_upload(server, 5)
        status, _, body = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Checksum": "md4 abcd",
                }
            ),
            b"hello",
        )
        assert status == 400
        assert b"md4" in body or b"Unsupported" in body

    def test_disabled_algorithm_rejected(self):
        # Server only enables sha1 — requesting sha256 must 400
        temp_dir = tempfile.mkdtemp()
        try:
            server = TusServer(
                storage=SQLiteStorage(
                    db_path=os.path.join(temp_dir, "u.db"),
                    upload_dir=os.path.join(temp_dir, "files"),
                ),
                base_path="/files",
                checksum_algorithms=("sha1",),
            )
            location = _create_upload(server, 5)
            digest = base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
            status, _, _ = server.handle_request(
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
            assert status == 400
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_unknown_algorithm_rejected_at_init(self):
        with pytest.raises(ValueError, match="Unknown"):
            TusServer(
                storage=SQLiteStorage(db_path=":memory:", upload_dir="/tmp/ru-test"),
                base_path="/files",
                checksum_algorithms=("rot13",),
            )


class TestClientMultiChecksum:
    """Client can pick the algorithm via Uploader(checksum='sha256')."""

    def _live_server(self, tmp_path, algorithms=("sha1", "sha256", "md5")):
        import threading
        from http.server import HTTPServer

        from resumable_upload.server import TusHTTPRequestHandler

        storage = SQLiteStorage(
            db_path=os.path.join(str(tmp_path), "u.db"),
            upload_dir=os.path.join(str(tmp_path), "files"),
        )
        tus = TusServer(
            storage=storage,
            base_path="/files",
            checksum_algorithms=algorithms,
        )

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = tus
        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f"http://127.0.0.1:{port}/files"

    def test_client_uploads_with_sha256(self, tmp_path):
        from resumable_upload.client import TusClient

        httpd, base_url = self._live_server(tmp_path)
        try:
            f = tmp_path / "x.bin"
            f.write_bytes(b"hello world")
            client = TusClient(base_url, chunk_size=1024, checksum="sha256")
            url = client.upload_file(str(f))
            info = client.get_upload_info(url)
            assert info["complete"] is True
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_client_checksum_false_sends_no_checksum_header(self, tmp_path):
        from resumable_upload.client import TusClient

        httpd, base_url = self._live_server(tmp_path)
        try:
            f = tmp_path / "x.bin"
            f.write_bytes(b"hello")
            client = TusClient(base_url, chunk_size=1024, checksum=False)
            url = client.upload_file(str(f))
            info = client.get_upload_info(url)
            assert info["complete"] is True
        finally:
            httpd.shutdown()
            httpd.server_close()
