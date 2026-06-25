"""End-to-end smoke tests for files under ``examples/``.

Spawns ``examples/server/http_server.py`` as a real subprocess and runs each
client example against it, asserting the SUCCESS banner. Also covers the
stale-stored-URL regression: re-running ``hooks.py``/``resume.py`` after the
server moved to a different port must fall back to a fresh upload.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_ready(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/files", method="OPTIONS")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 204:
                    return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.1)
    pytest.fail(f"example server did not come up on :{port}: {last_err}")


def _spawn_server(port: int, cwd: Path, script: str = "http_server.py") -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(EXAMPLES / "server" / script), str(port)],
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_until_ready(port)
    except Exception:
        proc.terminate()
        proc.wait(timeout=2)
        raise
    return proc


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _run_client(script: str, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(EXAMPLES / "client" / script), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Per-test cwd so server SQLite/uploads dir + client URL caches are isolated."""
    (tmp_path / "uploads").mkdir()
    return tmp_path


@pytest.fixture
def sample_file(workdir: Path) -> Path:
    p = workdir / "sample.bin"
    p.write_bytes(os.urandom(512 * 1024))
    return p


@pytest.fixture
def server(workdir: Path):
    port = _find_free_port()
    proc = _spawn_server(port, workdir)
    try:
        yield port
    finally:
        _stop(proc)


def _assert_success(result: subprocess.CompletedProcess, label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} exited with {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert "SUCCESS" in combined, f"{label} did not print SUCCESS banner:\n{combined}"


class TestClientExamples:
    def test_basic_upload(self, server: int, sample_file: Path, workdir: Path) -> None:
        result = _run_client(
            "basic_upload.py",
            [f"http://127.0.0.1:{server}/files", str(sample_file)],
            cwd=workdir,
        )
        _assert_success(result, "basic_upload")

    def test_low_level_uploader(self, server: int, sample_file: Path, workdir: Path) -> None:
        result = _run_client(
            "low_level_uploader.py",
            [f"http://127.0.0.1:{server}/files", str(sample_file)],
            cwd=workdir,
        )
        _assert_success(result, "low_level_uploader")

    def test_parallel_upload_auto(self, server: int, sample_file: Path, workdir: Path) -> None:
        result = _run_client(
            "parallel_upload.py",
            [f"http://127.0.0.1:{server}/files", str(sample_file), "2"],
            cwd=workdir,
        )
        _assert_success(result, "parallel_upload (auto)")

    def test_parallel_upload_manual(self, server: int, workdir: Path) -> None:
        result = _run_client(
            "parallel_upload.py",
            ["--manual", f"http://127.0.0.1:{server}/files"],
            cwd=workdir,
        )
        _assert_success(result, "parallel_upload (manual)")

    def test_hooks(self, server: int, sample_file: Path, workdir: Path) -> None:
        result = _run_client(
            "hooks.py",
            [f"http://127.0.0.1:{server}/files", str(sample_file)],
            cwd=workdir,
        )
        _assert_success(result, "hooks")
        # Stored URL should now exist in cwd.
        assert (workdir / ".hooks_example_urls.db").exists()

    def test_resume(self, server: int, sample_file: Path, workdir: Path) -> None:
        result = _run_client(
            "resume.py",
            [f"http://127.0.0.1:{server}/files", str(sample_file)],
            cwd=workdir,
        )
        _assert_success(result, "resume")
        assert (workdir / ".tus_urls.json").exists()


class TestStaleStoredURLFallback:
    """Regression: stored URLs persist across runs but are keyed only by file
    fingerprint, not server URL. If the server moves to a new host/port, the
    cached URL points at a dead address. The examples should drop the stale
    entry and retry against the fresh server instead of crashing.
    """

    def test_hooks_falls_back_when_stored_url_is_dead(
        self, sample_file: Path, workdir: Path
    ) -> None:
        # First run: server A populates the stored URL.
        port_a = _find_free_port()
        proc_a = _spawn_server(port_a, workdir)
        try:
            r1 = _run_client(
                "hooks.py",
                [f"http://127.0.0.1:{port_a}/files", str(sample_file)],
                cwd=workdir,
            )
            _assert_success(r1, "hooks first run")
        finally:
            _stop(proc_a)

        # Second run: server A is gone, server B is up on a different port.
        # Stored URL still points at A and HEAD will get connection-refused.
        port_b = _find_free_port()
        assert port_b != port_a
        proc_b = _spawn_server(port_b, workdir)
        try:
            r2 = _run_client(
                "hooks.py",
                [f"http://127.0.0.1:{port_b}/files", str(sample_file)],
                cwd=workdir,
            )
            _assert_success(r2, "hooks second run")
            assert "starting fresh" in r2.stdout, (
                f"hooks did not fall back to fresh upload:\n{r2.stdout}"
            )
        finally:
            _stop(proc_b)

    def test_resume_falls_back_when_stored_url_is_dead(
        self, sample_file: Path, workdir: Path
    ) -> None:
        port_a = _find_free_port()
        proc_a = _spawn_server(port_a, workdir)
        try:
            r1 = _run_client(
                "resume.py",
                [f"http://127.0.0.1:{port_a}/files", str(sample_file)],
                cwd=workdir,
            )
            _assert_success(r1, "resume first run")
        finally:
            _stop(proc_a)

        port_b = _find_free_port()
        assert port_b != port_a
        proc_b = _spawn_server(port_b, workdir)
        try:
            r2 = _run_client(
                "resume.py",
                [f"http://127.0.0.1:{port_b}/files", str(sample_file)],
                cwd=workdir,
            )
            _assert_success(r2, "resume second run")
            assert "starting fresh" in r2.stdout, (
                f"resume did not fall back to fresh upload:\n{r2.stdout}"
            )
        finally:
            _stop(proc_b)


class TestAsyncServerExamples:
    """The ASGI server examples drive ``handle_request_async`` over a real
    uvicorn event loop. ``asgi_app.py`` uses the default SQLite storage (async
    via the to_thread surface); ``async_storage.py`` uses a native-async
    backend. Both must serve the ordinary sync TUS client unchanged.
    """

    @pytest.fixture(params=["asgi_app.py", "async_storage.py"])
    def async_server(self, request, workdir: Path):
        pytest.importorskip("uvicorn")
        port = _find_free_port()
        proc = _spawn_server(port, workdir, script=request.param)
        try:
            yield port
        finally:
            _stop(proc)

    def test_basic_upload(self, async_server: int, sample_file: Path, workdir: Path) -> None:
        result = _run_client(
            "basic_upload.py",
            [f"http://127.0.0.1:{async_server}/files", str(sample_file)],
            cwd=workdir,
        )
        _assert_success(result, "basic_upload (async server)")

    def test_resume(self, async_server: int, sample_file: Path, workdir: Path) -> None:
        result = _run_client(
            "resume.py",
            [f"http://127.0.0.1:{async_server}/files", str(sample_file)],
            cwd=workdir,
        )
        _assert_success(result, "resume (async server)")


class TestFrameworkServerExamples:
    """Smoke-test the framework server example FILES themselves.

    The test_*_integration.py suites validate the protocol against each
    framework, but they build their own apps — the example files (flask_app.py
    etc.) are never executed there, so an import typo or wiring bug in an
    example would go unnoticed. Here we spawn each example as a real subprocess
    and run an actual upload through it. Frameworks are optional, so the cases
    skip when their dependency is absent (e.g. under the tox matrix).
    """

    @pytest.mark.parametrize(
        "script,modules",
        [
            ("flask_app.py", ["flask"]),
            ("fastapi_app.py", ["fastapi", "uvicorn"]),
            ("django_app.py", ["django"]),
        ],
    )
    def test_upload(
        self, script: str, modules: list[str], sample_file: Path, workdir: Path
    ) -> None:
        for module in modules:
            pytest.importorskip(module)
        port = _find_free_port()
        proc = _spawn_server(port, workdir, script=script)
        try:
            result = _run_client(
                "basic_upload.py",
                [f"http://127.0.0.1:{port}/files", str(sample_file)],
                cwd=workdir,
            )
            _assert_success(result, f"basic_upload ({script})")
        finally:
            _stop(proc)

    def test_with_metrics_example(self, sample_file: Path, workdir: Path) -> None:
        # Pure stdlib (in-memory lock by default) — runs everywhere, incl. tox.
        port = _find_free_port()
        proc = _spawn_server(port, workdir, script="with_metrics.py")
        try:
            result = _run_client(
                "basic_upload.py",
                [f"http://127.0.0.1:{port}/files", str(sample_file)],
                cwd=workdir,
            )
            _assert_success(result, "basic_upload (with_metrics)")
            # /metrics exposes Prometheus output and reflects the upload just made.
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
                assert resp.status == 200
                body = resp.read().decode()
            assert "tusd_requests_total" in body
            assert "tusd_uploads_created_total" in body
        finally:
            _stop(proc)
