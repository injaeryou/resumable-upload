"""Cross-implementation interop tests.

Each test class pairs one of our components with a real counterpart from the
TUS ecosystem, exercising only the features the counterpart implements:

- ``TestNativeRoundTrip``      — our server  <-> our client (always runs)
- ``TestClientAgainstTusd``    — tusd server <-> our client (needs ``tusd``)
- ``TestServerAgainstTusJs``   — our server  <-> tus-js-client (needs node)
- ``TestServerAgainstTusPy``   — our server  <-> tus-py-client (needs ``tuspy``)

Classes whose prerequisite is missing skip cleanly, so the default test run
stays green everywhere; ``make interop`` provisions the reference clients and
runs the lot. Override the tusd binary with ``TUSD_BIN``.
"""

import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from resumable_upload.client import TusClient
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage

INTEROP_DIR = os.path.join(os.path.dirname(__file__), "interop")
TUSD_BIN = os.environ.get("TUSD_BIN") or shutil.which("tusd")
NODE_BIN = shutil.which("node")
HAS_TUS_JS = os.path.isdir(os.path.join(INTEROP_DIR, "node_modules", "tus-js-client"))

try:
    from tusclient import client as tuspy_client  # tus-py-client (PyPI: tuspy)

    HAS_TUS_PY = True
except ImportError:
    HAS_TUS_PY = False

PAYLOAD = os.urandom(2 * 1024 * 1024 + 77)  # > chunk size, odd length
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="OPTIONS")
            with urllib.request.urlopen(req, timeout=1):
                return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError(f"server at {url} never came up")


def _download_sha(url: str) -> str:
    with urllib.request.urlopen(url) as resp:
        return hashlib.sha256(resp.read()).hexdigest()


@pytest.fixture
def ours_server():
    """resumable-upload TusServer over real HTTP; yields (base_url, storage)."""
    temp_dir = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(temp_dir, "u.db"),
        upload_dir=os.path.join(temp_dir, "files"),
    )
    tus = TusServer(
        storage=storage,
        base_path="/files",
        enable_downloads=True,
        supports_checksum_trailer=True,
        cors_allow_origins="*",
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/files", storage
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def payload_file(tmp_path):
    p = tmp_path / "payload.bin"
    p.write_bytes(PAYLOAD)
    return str(p)


class TestNativeRoundTrip:
    """our server <-> our client over real HTTP."""

    def test_roundtrip_single_and_parallel(self, ours_server, payload_file):
        base_url, _ = ours_server
        seen: list[str] = []
        client = TusClient(
            base_url,
            chunk_size=256 * 1024,
            add_request_id=True,
            on_upload_url_available=seen.append,
        )

        url = client.upload_file(payload_file)
        assert seen == [url]
        info = client.get_upload_info(url)
        assert info["offset"] == len(PAYLOAD)
        assert info["complete"] is True
        assert _download_sha(url) == PAYLOAD_SHA

        purl = client.upload_file(payload_file, parallel_uploads=3)
        assert seen[-1] == purl
        assert _download_sha(purl) == PAYLOAD_SHA


@pytest.mark.skipif(not TUSD_BIN, reason="tusd binary not found (set TUSD_BIN or add to PATH)")
class TestClientAgainstTusd:
    """our client <-> a real tusd server."""

    @pytest.fixture
    def tusd_server(self, tmp_path):
        port = _free_port()
        proc = subprocess.Popen(
            [
                TUSD_BIN,
                "-host",
                "127.0.0.1",
                "-port",
                str(port),
                "-upload-dir",
                str(tmp_path / "tusd-data"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}/files/"
        try:
            _wait_until_up(base)
            yield base
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_single_upload(self, tusd_server, payload_file):
        client = TusClient(tusd_server, chunk_size=256 * 1024, checksum=False)
        url = client.upload_file(payload_file, metadata={"filename": "ours.bin"})
        info = client.get_upload_info(url)
        assert info["offset"] == len(PAYLOAD)
        assert info["complete"] is True
        assert _download_sha(url) == PAYLOAD_SHA  # tusd serves GET downloads

    def test_parallel_concatenation(self, tusd_server, payload_file):
        seen: list[str] = []
        client = TusClient(
            tusd_server,
            chunk_size=256 * 1024,
            checksum=False,
            on_upload_url_available=seen.append,
        )
        url = client.upload_file(payload_file, parallel_uploads=3)
        assert seen == [url]
        assert _download_sha(url) == PAYLOAD_SHA

    def test_server_info(self, tusd_server):
        client = TusClient(tusd_server, checksum=False)
        info = client.get_server_info()
        assert "creation" in info["extensions"]
        assert "concatenation" in info["extensions"]

    def test_termination(self, tusd_server, payload_file):
        # tusd advertises the termination extension — our client's DELETE
        # must remove the upload wire-side.
        client = TusClient(tusd_server, chunk_size=256 * 1024, checksum=False)
        url = client.upload_file(payload_file, metadata={"filename": "ours.bin"})
        client.delete_upload(url)
        req = urllib.request.Request(url, method="HEAD", headers={"Tus-Resumable": "1.0.0"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 404

    # Skipped for tusd: it advertises neither the checksum nor the expiration
    # extension (get_server_info().extensions has no 'checksum'/'expiration'),
    # so there is nothing on the tusd side to interop against for those.


@pytest.mark.skipif(
    not (NODE_BIN and HAS_TUS_JS),
    reason="node or tus-js-client missing (run: npm install in tests/interop)",
)
class TestServerAgainstTusJs:
    """our server <-> the real tus-js-client (Node)."""

    def _run_lane3(self, base_url, mode):
        # The script lives inside tests/interop/ so ESM resolution finds the
        # node_modules installed next to it (NODE_PATH is ignored for ESM).
        return subprocess.run(
            [
                NODE_BIN,
                os.path.join(INTEROP_DIR, "tus_js_client.mjs"),
                base_url,
                str(len(PAYLOAD)),
                mode,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_js_upload_head_download(self, ours_server):
        base_url, _ = ours_server
        result = self._run_lane3(base_url, "roundtrip")
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("OK ")

    def test_js_termination(self, ours_server):
        # tus-js-client abort(true) issues the termination DELETE; our server
        # must then 404 the upload.
        base_url, _ = ours_server
        result = self._run_lane3(base_url, "terminate")
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("OK terminated ")


@pytest.mark.skipif(
    not HAS_TUS_PY,
    reason="tus-py-client missing (pip install tuspy)",
)
class TestServerAgainstTusPy:
    """our server <-> tus-py-client (official Python reference client).

    tusd ships no client binary, so the second reference client that
    exercises our *server* is the tus project's Python client.
    """

    def test_py_client_upload_and_download(self, ours_server, payload_file):
        base_url, _ = ours_server
        tc = tuspy_client.TusClient(base_url + "/")
        uploader = tc.uploader(
            payload_file,
            chunk_size=256 * 1024,
            metadata={"filename": "interop.bin"},
        )
        uploader.upload()
        url = uploader.url
        assert _download_sha(url) == PAYLOAD_SHA

    def test_py_client_resume_after_partial(self, ours_server, payload_file):
        # Resume is TUS's whole point: upload one chunk, drop the uploader,
        # then a fresh uploader must continue from the server's offset.
        base_url, _ = ours_server
        tc = tuspy_client.TusClient(base_url + "/")
        first = tc.uploader(payload_file, chunk_size=256 * 1024)
        first.upload_chunk()  # exactly one chunk
        assert 0 < first.offset < len(PAYLOAD)

        resumed = tc.uploader(payload_file, url=first.url, chunk_size=256 * 1024)
        resumed.upload()
        assert _download_sha(first.url) == PAYLOAD_SHA

    def test_py_client_checksum(self, ours_server, payload_file):
        # tus-py-client sends `Upload-Checksum: sha1 <b64>` per chunk when
        # upload_checksum=True; our server verifies it (checksum extension).
        base_url, _ = ours_server
        tc = tuspy_client.TusClient(base_url + "/")
        uploader = tc.uploader(
            payload_file,
            chunk_size=256 * 1024,
            metadata={"filename": "sum.bin"},
            upload_checksum=True,
        )
        uploader.upload()
        assert _download_sha(uploader.url) == PAYLOAD_SHA

    # Termination is skipped for this lane: tus-py-client exposes no
    # delete/terminate call, so there is nothing client-side to drive it.
