"""Location URL construction: relative default, location_base_url, behind_proxy."""

import os
import shutil
import tempfile

import pytest

from resumable_upload.server import TusServer
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


def _post(server, extra_headers=None):
    headers = {"Tus-Resumable": "1.0.0", "Upload-Length": "5"}
    headers.update(extra_headers or {})
    return server.handle_request("POST", "/files", headers, b"")


class TestDefaultRelative:
    def test_location_stays_relative(self, storage):
        server = TusServer(storage=storage, base_path="/files")
        status, headers, _ = _post(server, {"Host": "upload.example"})
        assert status == 201
        assert headers["Location"].startswith("/files/")


class TestLocationBaseUrl:
    def test_explicit_base_url_prefixes_location(self, storage):
        server = TusServer(
            storage=storage, base_path="/files", location_base_url="https://cdn.example"
        )
        _, headers, _ = _post(server)
        assert headers["Location"].startswith("https://cdn.example/files/")

    def test_trailing_slash_normalized(self, storage):
        server = TusServer(
            storage=storage, base_path="/files", location_base_url="https://cdn.example/"
        )
        _, headers, _ = _post(server)
        assert headers["Location"].startswith("https://cdn.example/files/")
        assert "//files" not in headers["Location"].replace("https://", "")


class TestBehindProxy:
    @pytest.fixture
    def server(self, storage):
        return TusServer(storage=storage, base_path="/files", behind_proxy=True)

    def test_forwarded_proto_and_host(self, server):
        _, headers, _ = _post(
            server,
            {
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "upload.example",
                "Host": "10.0.0.5:8080",
            },
        )
        assert headers["Location"].startswith("https://upload.example/files/")

    def test_host_header_fallback(self, server):
        _, headers, _ = _post(server, {"Host": "upload.example:8080"})
        assert headers["Location"].startswith("http://upload.example:8080/files/")

    def test_no_host_falls_back_to_relative(self, server):
        _, headers, _ = _post(server)
        assert headers["Location"].startswith("/files/")

    def test_forwarded_proto_list_uses_first(self, server):
        _, headers, _ = _post(
            server,
            {"X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "upload.example"},
        )
        assert headers["Location"].startswith("https://upload.example/files/")

    def test_final_concat_location_absolute(self, server):
        # Build two complete partials, then POST the final with proxy headers.
        locs = []
        for data in (b"hi", b"yo"):
            _, h, _ = server.handle_request(
                "POST",
                "/files",
                {
                    "Tus-Resumable": "1.0.0",
                    "Upload-Length": "2",
                    "Upload-Concat": "partial",
                },
                b"",
            )
            server.handle_request(
                "PATCH",
                h["Location"],
                {
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                data,
            )
            locs.append(h["Location"])

        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            {
                "Tus-Resumable": "1.0.0",
                "Upload-Concat": f"final;{locs[0]} {locs[1]}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "upload.example",
            },
            b"",
        )
        assert status == 201
        assert headers["Location"].startswith("https://upload.example/files/")

    @pytest.mark.anyio
    async def test_async_forwarded(self, server):
        status, headers, _ = await server.handle_request_async(
            "POST",
            "/files",
            {
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "5",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "upload.example",
            },
            b"",
        )
        assert status == 201
        assert headers["Location"].startswith("https://upload.example/files/")
