"""Smoke tests for the resumable-upload CLI."""

import socket
import subprocess
import sys
import time
import urllib.request

import pytest


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestCLI:
    def test_cli_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "resumable_upload", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "serve" in result.stdout

    def test_cli_serve_subcommand_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "resumable_upload", "serve", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "--host" in result.stdout
        assert "--port" in result.stdout
        assert "--upload-dir" in result.stdout
        assert "--db-path" in result.stdout

    def test_cli_no_command_shows_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "resumable_upload"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0  # missing required subcommand

    def test_cli_serve_starts_and_responds(self, tmp_path):
        port = _find_free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "resumable_upload",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--upload-dir",
                str(tmp_path / "uploads"),
                "--db-path",
                str(tmp_path / "u.db"),
                "--log-level",
                "WARNING",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5
            last_err: Exception | None = None
            while time.time() < deadline:
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/files",
                        method="OPTIONS",
                    )
                    with urllib.request.urlopen(req, timeout=0.5) as resp:
                        assert resp.status == 204
                        extensions = resp.headers.get("Tus-Extension", "")
                        assert "creation" in extensions
                        assert "concatenation" in extensions
                        return
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    time.sleep(0.1)
            pytest.fail(f"CLI server did not come up in time: {last_err}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_cli_serve_respects_cors_and_max_size(self, tmp_path):
        port = _find_free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "resumable_upload",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--upload-dir",
                str(tmp_path / "uploads"),
                "--db-path",
                str(tmp_path / "u.db"),
                "--max-size",
                "1024",
                "--cors-origin",
                "*",
                "--log-level",
                "WARNING",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/files",
                        method="OPTIONS",
                    )
                    with urllib.request.urlopen(req, timeout=0.5) as resp:
                        assert resp.headers.get("Tus-Max-Size") == "1024"
                        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
                        return
                except Exception:  # noqa: BLE001
                    time.sleep(0.1)
            pytest.fail("CLI server did not come up in time")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_cli_serve_cors_credentials_max_age_and_checksums(self, tmp_path):  # noqa: PLR0915
        port = _find_free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "resumable_upload",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--upload-dir",
                str(tmp_path / "uploads"),
                "--db-path",
                str(tmp_path / "u.db"),
                "--cors-origin",
                "*",
                "--cors-credentials",
                "--cors-max-age",
                "600",
                "--checksum-algorithms",
                "sha1,sha256",
                "--log-level",
                "WARNING",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/files",
                        method="OPTIONS",
                        headers={"Origin": "https://app.example"},
                    )
                    with urllib.request.urlopen(req, timeout=0.5) as resp:
                        assert resp.headers.get("Access-Control-Allow-Credentials") == "true"
                        assert resp.headers.get("Access-Control-Max-Age") == "600"
                        assert "sha256" in resp.headers.get("Tus-Checksum-Algorithm", "")
                        return
                except AssertionError:
                    raise
                except Exception:  # noqa: BLE001
                    time.sleep(0.1)
            pytest.fail("CLI server did not come up in time")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture
def cli_server(tmp_path):
    """In-process TUS server (downloads on) for the client-side CLI tests."""
    import threading
    from http.server import HTTPServer

    from resumable_upload.server import TusHTTPRequestHandler, TusServer
    from resumable_upload.storage import SQLiteStorage

    tus = TusServer(
        storage=SQLiteStorage(db_path=str(tmp_path / "u.db"), upload_dir=str(tmp_path / "f")),
        base_path="/files",
        enable_downloads=True,
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/files"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "resumable_upload", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestCLIClient:
    def test_upload_info_download_roundtrip(self, cli_server, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"Z" * 5000)

        up = _cli(
            "upload",
            str(src),
            "--url",
            cli_server,
            "--chunk-size",
            "1024",
            "--metadata",
            "author=me",
            "--no-progress",
        )
        assert up.returncode == 0, up.stderr
        url = up.stdout.strip().splitlines()[-1]
        assert url.startswith(cli_server + "/")

        inf = _cli("info", url)
        assert inf.returncode == 0, inf.stderr
        assert "complete: True" in inf.stdout
        assert "author: me" in inf.stdout
        assert "filename: src.bin" in inf.stdout

        out = tmp_path / "out.bin"
        dl = _cli("download", url, "-o", str(out))
        assert dl.returncode == 0, dl.stderr
        assert out.read_bytes() == b"Z" * 5000

    def test_upload_parallel(self, cli_server, tmp_path):
        src = tmp_path / "big.bin"
        src.write_bytes(b"Q" * 20000)
        up = _cli("upload", str(src), "--url", cli_server, "--parallel", "3", "--no-progress")
        assert up.returncode == 0, up.stderr
        url = up.stdout.strip().splitlines()[-1]
        out = tmp_path / "o.bin"
        assert _cli("download", url, "-o", str(out)).returncode == 0
        assert out.read_bytes() == b"Q" * 20000

    def test_upload_bad_metadata(self, cli_server, tmp_path):
        src = tmp_path / "s.bin"
        src.write_bytes(b"x")
        up = _cli("upload", str(src), "--url", cli_server, "--metadata", "novalue", "--no-progress")
        assert up.returncode != 0
        assert "KEY=VALUE" in up.stderr
