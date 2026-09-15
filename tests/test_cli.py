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


def _cli(*args: str, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "resumable_upload", *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
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


class TestCLIClientErrorsAndResume:
    def test_version(self):
        from resumable_upload import __version__

        r = _cli("--version")
        assert r.returncode == 0, r.stderr
        assert __version__ in r.stdout

    def test_upload_resume_reuses_upload_url(self, cli_server, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"R" * 5000)
        args = ("upload", str(src), "--url", cli_server, "--resume", "--no-progress")
        first = _cli(*args, cwd=tmp_path)
        assert first.returncode == 0, first.stderr
        second = _cli(*args, cwd=tmp_path)
        assert second.returncode == 0, second.stderr
        assert first.stdout.strip() == second.stdout.strip()
        assert (tmp_path / ".tus_urls.json").exists()

    def test_upload_resume_recreates_when_server_forgot_upload(self, cli_server, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"R" * 5000)
        args = ("upload", str(src), "--url", cli_server, "--resume", "--no-progress")
        first = _cli(*args, cwd=tmp_path)
        assert first.returncode == 0, first.stderr
        url = first.stdout.strip()
        req = urllib.request.Request(url, headers={"Tus-Resumable": "1.0.0"}, method="DELETE")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 204
        second = _cli(*args, cwd=tmp_path)
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip().startswith(cli_server + "/")
        assert second.stdout.strip() != url

    def test_upload_resume_with_parallel_reuses_final_url(self, cli_server, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"P" * 5000)
        args = ("upload", str(src), "--url", cli_server, "--resume", "--parallel", "2")
        first = _cli(*args, "--no-progress", cwd=tmp_path)
        assert first.returncode == 0, first.stderr
        second = _cli(*args, "--no-progress", cwd=tmp_path)
        assert second.returncode == 0, second.stderr
        assert first.stdout.strip() == second.stdout.strip()

    def test_upload_missing_file_is_a_one_line_error(self, cli_server, tmp_path):
        r = _cli("upload", str(tmp_path / "nope.bin"), "--url", cli_server, "--no-progress")
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "nope.bin" in r.stderr

    def test_upload_connection_refused_is_a_one_line_error(self, tmp_path):
        src = tmp_path / "s.bin"
        src.write_bytes(b"x")
        url = f"http://127.0.0.1:{_find_free_port()}/files"
        r = _cli("upload", str(src), "--url", url, "--no-progress")
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "Failed to create upload" in r.stderr

    def test_info_and_download_missing_upload_are_one_line_errors(self, cli_server, tmp_path):
        url = cli_server + "/00000000-0000-4000-8000-000000000000"
        inf = _cli("info", url)
        assert inf.returncode == 1
        assert "Traceback" not in inf.stderr
        assert "404" in inf.stderr

        out = tmp_path / "x.bin"
        dl = _cli("download", url, "-o", str(out))
        assert dl.returncode == 1
        assert "Traceback" not in dl.stderr
        assert "404" in dl.stderr
        assert not out.exists()

    def test_upload_rejects_unknown_checksum_up_front(self, cli_server, tmp_path):
        src = tmp_path / "s.bin"
        src.write_bytes(b"x")
        r = _cli("upload", str(src), "--url", cli_server, "--checksum", "bogus")
        assert r.returncode == 2
        assert "invalid choice" in r.stderr

    def test_upload_unsupported_checksum_fails_fast(self, cli_server, tmp_path):
        """The server accepts sha1 only; its 400 is deterministic and must not be retried."""
        src = tmp_path / "s.bin"
        src.write_bytes(b"x" * 100)
        start = time.time()
        r = _cli("upload", str(src), "--url", cli_server, "--checksum", "sha256", "--no-progress")
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "400" in r.stderr
        assert time.time() - start < 5  # retry backoff would be 1s + 2s + 4s

    def test_progress_is_silent_when_stderr_is_not_a_tty(self, cli_server, tmp_path):
        src = tmp_path / "s.bin"
        src.write_bytes(b"x" * 5000)
        r = _cli("upload", str(src), "--url", cli_server, "--chunk-size", "1024")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().startswith(cli_server + "/")
        assert "%" not in r.stdout
        assert "%" not in r.stderr

    def test_progress_goes_to_stderr_on_a_tty(self, cli_server, tmp_path, capsys, monkeypatch):
        import io

        from resumable_upload import cli

        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        err = Tty()
        monkeypatch.setattr(sys, "stderr", err)
        src = tmp_path / "s.bin"
        src.write_bytes(b"x" * 5000)
        rc = cli.main(["upload", str(src), "--url", cli_server, "--chunk-size", "1024"])
        assert rc == 0
        assert "%" in err.getvalue()
        assert capsys.readouterr().out.strip().startswith(cli_server + "/")

    def test_serve_port_in_use_is_a_one_line_error(self, tmp_path):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            r = subprocess.run(
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
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "Address already in use" in r.stderr

    def test_serve_redis_without_extra_points_at_the_extra(self, tmp_path, monkeypatch):
        from resumable_upload import cli

        monkeypatch.setitem(sys.modules, "redis", None)  # simulate: extra not installed
        with pytest.raises(SystemExit) as ei:
            cli.main(
                [
                    "serve",
                    "--lock-backend",
                    "redis",
                    "--redis-url",
                    "redis://localhost:6379/0",
                    "--upload-dir",
                    str(tmp_path / "uploads"),
                    "--db-path",
                    str(tmp_path / "u.db"),
                ]
            )
        assert "resumable-upload[redis]" in str(ei.value)


@pytest.fixture
def auth_cli_server(tmp_path):
    """In-process TUS server that rejects every request lacking a bearer token."""
    import threading

    from resumable_upload.cli import _ThreadingHTTPServer
    from resumable_upload.exceptions import TusHookError
    from resumable_upload.server import TusHTTPRequestHandler, TusServer
    from resumable_upload.storage import SQLiteStorage

    def require_token(method, path, headers):
        if headers.get("authorization") != "Bearer s3cret":
            raise TusHookError("missing token", status_code=401)

    tus = TusServer(
        storage=SQLiteStorage(db_path=str(tmp_path / "u.db"), upload_dir=str(tmp_path / "f")),
        base_path="/files",
        enable_downloads=True,
        on_incoming_request=require_token,
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus
    httpd = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/files"
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestCLIHeaders:
    def test_header_is_sent_on_upload_info_and_download(self, auth_cli_server, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"H" * 3000)
        auth = ("--header", "Authorization=Bearer s3cret")

        denied = _cli("upload", str(src), "--url", auth_cli_server, "--no-progress")
        assert denied.returncode == 1
        assert "401" in denied.stderr

        up = _cli("upload", str(src), "--url", auth_cli_server, "--no-progress", *auth)
        assert up.returncode == 0, up.stderr
        url = up.stdout.strip()

        assert _cli("info", url).returncode == 1
        inf = _cli("info", url, *auth)
        assert inf.returncode == 0, inf.stderr
        assert "complete: True" in inf.stdout

        out = tmp_path / "out.bin"
        assert _cli("download", url, "-o", str(out)).returncode == 1
        dl = _cli("download", url, "-o", str(out), *auth)
        assert dl.returncode == 0, dl.stderr
        assert out.read_bytes() == b"H" * 3000

    @pytest.mark.parametrize(
        ("redirect_host", "expected_rc"),
        [("127.0.0.1", 0), ("localhost", 1)],
        ids=["same-host-keeps-auth", "cross-host-drops-auth"],
    )
    def test_download_redirect_forwards_auth_only_to_the_same_host(
        self, auth_cli_server, tmp_path, redirect_host, expected_rc
    ):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import urlsplit

        src = tmp_path / "src.bin"
        src.write_bytes(b"H" * 100)
        auth = ("--header", "Authorization=Bearer s3cret")
        up = _cli("upload", str(src), "--url", auth_cli_server, "--no-progress", *auth)
        assert up.returncode == 0, up.stderr
        target = urlsplit(up.stdout.strip())
        location = target._replace(netloc=f"{redirect_host}:{target.port}").geturl()

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # noqa: ARG002
                pass

        hop = HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=hop.serve_forever, daemon=True).start()
        try:
            out = tmp_path / "out.bin"
            dl = _cli(
                "download", f"http://127.0.0.1:{hop.server_address[1]}/x", "-o", str(out), *auth
            )
        finally:
            hop.shutdown()
            hop.server_close()
        assert dl.returncode == expected_rc, dl.stderr
        if expected_rc == 0:
            assert out.read_bytes() == b"H" * 100
        else:
            assert "401" in dl.stderr

    def test_header_must_be_key_value(self, cli_server, tmp_path):
        src = tmp_path / "s.bin"
        src.write_bytes(b"x")
        r = _cli("upload", str(src), "--url", cli_server, "--header", "novalue")
        assert r.returncode == 1
        assert "--header must be KEY=VALUE" in r.stderr


class TestCLIParity:
    """Every scalar constructor option of the server and the client must be
    reachable from the CLI (CLAUDE.md, Feature Surface Checklist). Objects and
    callables are wired in code, not on a command line, and are listed here."""

    SERVER_NOT_FOR_CLI = {
        "storage",  # SQLiteStorage is built from --upload-dir/--db-path
        "metrics_registry",  # created when --metrics-path is set
        "lock_backend",  # chosen via --lock-backend/--redis-url
        "supports_checksum_trailer",  # property of the bundled transport, always on
        "on_incoming_request",
        "on_upload_create",
        "on_upload_complete",
        "on_upload_terminate",
        "on_chunk_received",
        "on_before_terminate",
    }
    CLIENT_NOT_FOR_CLI = {
        "url",  # positional --url
        "url_storage",  # FileURLStorage is implied by --resume
        "fingerprinter",
        "before_request",
        "after_response",
        "on_should_retry",
        "on_upload_url_available",
    }
    # constructor kwarg -> argparse dest when the names differ
    SERVER_ALIASES = {
        "cors_allow_origins": "cors_origin",
        "cors_allow_credentials": "cors_credentials",
        "lock_ttl_seconds": "lock_ttl",
        "lock_wait_seconds": "lock_wait",
    }
    CLIENT_ALIASES = {
        "store_url": "resume",
        "headers": "header",
        "verify_tls_cert": "insecure",
        "add_request_id": "request_id",
    }

    @staticmethod
    def _dests(command: str) -> set[str]:
        import argparse

        from resumable_upload.cli import _build_parser

        subparsers = next(
            a for a in _build_parser()._actions if isinstance(a, argparse._SubParsersAction)
        )
        return {a.dest for a in subparsers.choices[command]._actions if a.dest != "help"}

    @staticmethod
    def _params(cls) -> set[str]:
        import inspect

        return set(inspect.signature(cls.__init__).parameters) - {"self"}

    def test_upload_flags_reach_the_client(self, monkeypatch, tmp_path):
        """Each client-side flag lands on the TusClient kwarg it stands for."""
        from resumable_upload import cli

        seen: dict = {}

        class FakeClient:
            def __init__(self, url, **kwargs):
                seen.update(kwargs)

            def upload_file(self, *a, **k):
                return "http://h/files/x"

        monkeypatch.setattr("resumable_upload.client.TusClient", FakeClient)
        src = tmp_path / "s.bin"
        src.write_bytes(b"x")
        argv = [
            "upload", str(src), "--url", "http://h/files", "--no-progress",
            "--timeout", "7.5", "--insecure", "--max-retries", "5", "--retry-delay", "0.2",
            "--override-patch-method", "--request-id", "--metadata-encoding", "latin-1",
            "--header", "X-A=1", "--resume",
        ]  # fmt: skip
        assert cli.main(argv) == 0
        assert seen["timeout"] == 7.5
        assert seen["verify_tls_cert"] is False
        assert seen["max_retries"] == 5
        assert seen["retry_delay"] == 0.2
        assert seen["override_patch_method"] is True
        assert seen["add_request_id"] is True
        assert seen["metadata_encoding"] == "latin-1"
        assert seen["headers"] == {"X-A": "1"}
        assert seen["store_url"] is True

    def test_serve_exposes_every_scalar_server_option(self):
        from resumable_upload.server import TusServerCore

        missing = {
            p
            for p in self._params(TusServerCore) - self.SERVER_NOT_FOR_CLI
            if self.SERVER_ALIASES.get(p, p) not in self._dests("serve")
        }
        assert not missing, f"TusServer options without a `serve` flag: {sorted(missing)}"

    def test_upload_exposes_every_scalar_client_option(self):
        from resumable_upload.client import TusClient

        missing = {
            p
            for p in self._params(TusClient) - self.CLIENT_NOT_FOR_CLI
            if self.CLIENT_ALIASES.get(p, p) not in self._dests("upload")
        }
        assert not missing, f"TusClient options without an `upload` flag: {sorted(missing)}"
