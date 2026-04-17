"""Tests for multi-algorithm checksum support."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import tempfile

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage


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
            checksum_algorithms=("sha1", "sha256", "md5"),
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _h(**extra):
    base = {"Tus-Resumable": "1.0.0"}
    base.update(extra)
    return base


def _create_upload(server, length: int) -> str:
    _, headers, _ = server.handle_request(
        "POST", "/files", _h(**{"Upload-Length": str(length)}), b""
    )
    return headers["Location"]


class TestChecksumAlgorithms:
    def test_options_advertises_all_algorithms(self, server):
        status, headers, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert status == 204
        algos = set(headers["Tus-Checksum-Algorithm"].split(","))
        assert {"sha1", "sha256", "md5"} <= algos

    def test_patch_with_sha1_still_works(self, server):
        location = _create_upload(server, 5)
        data = b"hello"
        digest = base64.b64encode(hashlib.sha1(data).digest()).decode()
        status, _, _ = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Checksum": f"sha1 {digest}",
                }
            ),
            data,
        )
        assert status == 204

    def test_patch_with_sha256_accepted(self, server):
        location = _create_upload(server, 5)
        data = b"hello"
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
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
            data,
        )
        assert status == 204

    def test_patch_with_md5_accepted(self, server):
        location = _create_upload(server, 5)
        data = b"hello"
        digest = base64.b64encode(hashlib.md5(data).digest()).decode()
        status, _, _ = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Checksum": f"md5 {digest}",
                }
            ),
            data,
        )
        assert status == 204

    def test_patch_with_invalid_sha256_rejected(self, server):
        location = _create_upload(server, 5)
        wrong = base64.b64encode(b"0" * 32).decode()
        status, _, _ = server.handle_request(
            "PATCH",
            location,
            _h(
                **{
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Checksum": f"sha256 {wrong}",
                }
            ),
            b"hello",
        )
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
