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


def _spawn_serve(tmp_path, port: int, *extra: str) -> subprocess.Popen:
    """Start `resumable-upload serve` on ``port`` with output discarded."""
    return subprocess.Popen(
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
            *extra,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop(proc: subprocess.Popen) -> None:
    """Terminate the serve subprocess, escalating to SIGKILL, and reap it."""
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_until_serving(proc: subprocess.Popen, port: int, timeout: float = 5.0) -> None:
    """Block until the serve subprocess answers on ``port``."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/files", method="OPTIONS")
            with urllib.request.urlopen(req, timeout=0.5):
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.1)
    proc.terminate()
    pytest.fail(f"CLI server did not come up in time: {last_err}")


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

    from resumable_upload.cli import _ThreadingHTTPServer
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
    httpd = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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


class TestCLIServeConcurrency:
    def test_cli_serve_handles_concurrent_requests(self, tmp_path):
        """A stalled connection must not block other clients.

        A single-threaded ``HTTPServer`` blocks inside ``readline()`` on the
        half-sent request until ``request_timeout`` elapses, so the second
        client times out.
        """
        port = _find_free_port()
        proc = _spawn_serve(tmp_path, port)
        stalled = socket.socket()
        try:
            _wait_until_serving(proc, port)

            # Client 1: send a request line, then go quiet mid-request.
            stalled.connect(("127.0.0.1", port))
            stalled.sendall(b"OPTIONS /files HTTP/1.1\r\nHost: 127.0.0.1\r\n")

            # Client 2 must still be served while client 1 holds its thread.
            req = urllib.request.Request(f"http://127.0.0.1:{port}/files", method="OPTIONS")
            with urllib.request.urlopen(req, timeout=3) as resp:
                assert resp.status == 204
        finally:
            stalled.close()
            _stop(proc)

    def test_cli_serve_exits_on_sigterm_with_a_stalled_connection(self, tmp_path):
        """SIGTERM must actually terminate the process.

        ``httpd.shutdown()`` blocks until ``serve_forever()`` returns, so calling
        it inline from the signal handler — which interrupts the very thread
        running ``serve_forever()`` — deadlocks and only SIGKILL gets out.
        """
        port = _find_free_port()
        proc = _spawn_serve(tmp_path, port, "--request-timeout", "2")
        stalled = socket.socket()
        try:
            _wait_until_serving(proc, port)
            stalled.connect(("127.0.0.1", port))
            stalled.sendall(b"OPTIONS /files HTTP/1.1\r\nHost: 127.0.0.1\r\n")

            proc.terminate()
            # Drains, bounded by --request-timeout reaping the stalled thread.
            assert proc.wait(timeout=15) is not None
        finally:
            stalled.close()
            _stop(proc)

    def test_serve_drains_in_flight_request_on_shutdown(self):
        """``server_close()`` waits for a request already being served."""
        import threading
        from http.server import BaseHTTPRequestHandler

        from resumable_upload.cli import _ThreadingHTTPServer

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        class SlowHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802
                started.set()
                release.wait(5)
                body = b"drained"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                finished.set()

            def log_message(self, *args):
                pass

        httpd = _ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        port = httpd.server_address[1]
        result: list[bytes] = []
        timer = threading.Timer(0.5, release.set)

        def client():
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as resp:
                result.append(resp.read())

        caller = threading.Thread(target=client)
        try:
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            caller.start()
            assert started.wait(5), "handler never started"

            httpd.shutdown()
            timer.start()
            t0 = time.monotonic()
            httpd.server_close()  # must block until the in-flight handler returns
            blocked_for = time.monotonic() - t0

            assert finished.is_set(), "server_close() abandoned an in-flight request"
            assert blocked_for > 0.2, f"server_close() returned early ({blocked_for:.3f}s)"
        finally:
            timer.cancel()
            release.set()
            caller.join(5)
        assert result == [b"drained"]


class TestServeTuningFlags:
    """Deployer knobs on TusServerCore must be reachable from `serve`.

    CLAUDE.md calls out this gap class: an option lands on the server and the
    CLI never grows a flag for it.
    """

    def _capture_server_kwargs(self, monkeypatch, tmp_path, *flags: str) -> dict:
        from resumable_upload import cli

        captured: dict = {}

        class FakeServer:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class FakeHTTPD:
            def __init__(self, addr, handler):
                pass

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        monkeypatch.setattr(cli, "TusServer", FakeServer)
        monkeypatch.setattr(cli, "_ThreadingHTTPServer", FakeHTTPD)
        # _serve installs SIGINT/SIGTERM handlers bound to the httpd it built.
        # Left in place they outlive the test and replace pytest's own Ctrl+C.
        monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
        argv = [
            "serve",
            "--db-path",
            str(tmp_path / "u.db"),
            "--upload-dir",
            str(tmp_path / "uploads"),  # the default would land in the CWD
            *flags,
        ]
        cli._serve(cli._build_parser().parse_args(argv))
        return captured

    def test_defaults_match_the_server_defaults(self, monkeypatch, tmp_path):
        kwargs = self._capture_server_kwargs(monkeypatch, tmp_path)
        assert kwargs["cleanup_interval"] == 60
        assert kwargs["lock_ttl_seconds"] == 60.0
        assert kwargs["lock_wait_seconds"] == 5.0

    def test_flags_reach_the_server(self, monkeypatch, tmp_path):
        kwargs = self._capture_server_kwargs(
            monkeypatch,
            tmp_path,
            "--cleanup-interval",
            "5",
            "--lock-ttl",
            "12.5",
            "--lock-wait",
            "0.25",
        )
        assert kwargs["cleanup_interval"] == 5
        assert kwargs["lock_ttl_seconds"] == 12.5
        assert kwargs["lock_wait_seconds"] == 0.25
