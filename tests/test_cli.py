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

    def test_cli_serve_cors_credentials_max_age_and_checksums(self, tmp_path):
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
