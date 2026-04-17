"""Tests for the opt-in Prometheus metrics registry and /metrics endpoint."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from http.server import HTTPServer

import pytest

from resumable_upload.metrics import MetricsRegistry
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


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


class TestMetricsRegistry:
    def test_counter_increment_simple(self):
        reg = MetricsRegistry()
        reg.inc("tusd_requests_total", labels={"method": "POST"})
        reg.inc("tusd_requests_total", labels={"method": "POST"})
        reg.inc("tusd_requests_total", labels={"method": "PATCH"})
        text = reg.render()
        assert 'tusd_requests_total{method="POST"} 2' in text
        assert 'tusd_requests_total{method="PATCH"} 1' in text

    def test_counter_without_labels(self):
        reg = MetricsRegistry()
        reg.inc("tusd_bytes_received_total", value=1234)
        reg.inc("tusd_bytes_received_total", value=1)
        text = reg.render()
        assert "tusd_bytes_received_total 1235" in text

    def test_help_and_type_lines_emitted(self):
        reg = MetricsRegistry()
        reg.register_counter("tusd_bytes_received_total", "Total bytes received by the server")
        reg.inc("tusd_bytes_received_total", value=42)
        text = reg.render()
        assert "# HELP tusd_bytes_received_total Total bytes received by the server" in text
        assert "# TYPE tusd_bytes_received_total counter" in text
        assert "tusd_bytes_received_total 42" in text

    def test_labels_sorted_for_deterministic_output(self):
        reg = MetricsRegistry()
        reg.inc("metric", labels={"b": "2", "a": "1"})
        text = reg.render()
        # Labels rendered in sorted key order so scrape output is stable
        assert 'metric{a="1",b="2"} 1' in text

    def test_thread_safe_under_contention(self):
        reg = MetricsRegistry()
        reg.register_counter("counter", "test")

        def worker():
            for _ in range(1000):
                reg.inc("counter")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        text = reg.render()
        assert "counter 8000" in text


class TestServerMetricsIntegration:
    def test_server_emits_basic_request_counter(self, storage):
        metrics = MetricsRegistry()
        server = TusServer(storage=storage, base_path="/files", metrics_registry=metrics)
        server.handle_request("OPTIONS", "/files", {}, b"")
        server.handle_request(
            "POST",
            "/files",
            {"Tus-Resumable": "1.0.0", "Upload-Length": "5"},
            b"",
        )
        text = metrics.render()
        assert 'tusd_requests_total{method="OPTIONS"} 1' in text
        assert 'tusd_requests_total{method="POST"} 1' in text
        assert "tusd_uploads_created_total 1" in text

    def test_server_emits_bytes_received_on_patch(self, storage):
        metrics = MetricsRegistry()
        server = TusServer(storage=storage, base_path="/files", metrics_registry=metrics)
        _, headers, _ = server.handle_request(
            "POST",
            "/files",
            {"Tus-Resumable": "1.0.0", "Upload-Length": "5"},
            b"",
        )
        location = headers["Location"]
        server.handle_request(
            "PATCH",
            location,
            {
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            b"hello",
        )
        text = metrics.render()
        assert "tusd_bytes_received_total 5" in text
        assert "tusd_uploads_finished_total 1" in text

    def test_server_emits_error_counter(self, storage):
        metrics = MetricsRegistry()
        server = TusServer(storage=storage, base_path="/files", metrics_registry=metrics)
        server.handle_request(
            "HEAD", "/files/00000000-0000-0000-0000-000000000000", {"Tus-Resumable": "1.0.0"}, b""
        )
        text = metrics.render()
        assert 'tusd_errors_total{status="404"}' in text

    def test_server_emits_terminated_counter(self, storage):
        metrics = MetricsRegistry()
        server = TusServer(storage=storage, base_path="/files", metrics_registry=metrics)
        _, headers, _ = server.handle_request(
            "POST",
            "/files",
            {"Tus-Resumable": "1.0.0", "Upload-Length": "5"},
            b"",
        )
        location = headers["Location"]
        server.handle_request("DELETE", location, {"Tus-Resumable": "1.0.0"}, b"")
        text = metrics.render()
        assert "tusd_uploads_terminated_total 1" in text

    def test_server_without_registry_is_silent(self, storage):
        # Sanity: default TusServer does not crash or require metrics.
        server = TusServer(storage=storage, base_path="/files")
        status, _, _ = server.handle_request("OPTIONS", "/files", {}, b"")
        assert status == 204


class TestMetricsHTTPEndpoint:
    def test_metrics_path_served_by_http_handler(self, storage):
        import urllib.request

        metrics = MetricsRegistry()
        tus = TusServer(
            storage=storage,
            base_path="/files",
            metrics_registry=metrics,
            metrics_path="/metrics",
        )

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = tus
        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # Prime a request so at least one counter is non-zero
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/files", method="OPTIONS"),
                timeout=2,
            )
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
                assert resp.status == 200
                body = resp.read().decode()
                assert 'tusd_requests_total{method="OPTIONS"} 1' in body
                ctype = resp.headers["Content-Type"]
                assert ctype.startswith("text/plain")
        finally:
            server.shutdown()
            server.server_close()

    def test_metrics_disabled_by_default_returns_404(self, storage):
        import urllib.request
        from urllib.error import HTTPError

        tus = TusServer(storage=storage, base_path="/files")

        class Handler(TusHTTPRequestHandler):
            pass

        Handler.tus_server = tus
        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with pytest.raises(HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2)
            assert exc.value.code == 404
        finally:
            server.shutdown()
            server.server_close()
