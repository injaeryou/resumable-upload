"""
Test script to verify Python version compatibility.

This script tests the resumable-upload library on the current Python version
to ensure it works correctly across Python 3.11, 3.12, 3.13, 3.14, and 3.15.
"""

import os
import sys
import tempfile
import time
from http.server import HTTPServer
from threading import Thread

from resumable_upload import TusClient, TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


class TestPythonCompatibility:
    """Test library compatibility with current Python version."""

    def test_import_modules(self):
        """Test 1: Import modules."""
        # This test verifies that all modules can be imported
        assert TusClient is not None
        assert TusHTTPRequestHandler is not None
        assert TusServer is not None
        assert SQLiteStorage is not None

    def test_tus_version_constants(self):
        """Test 2: Check TUS protocol version."""
        assert TusServer.TUS_VERSION == "1.0.0", "Server version mismatch"
        assert TusClient.TUS_VERSION == "1.0.0", "Client version mismatch"

    def test_create_instances(self):
        """Test 3: Create server and client instances."""
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "test.db")
            upload_dir = os.path.join(tmpdir, "uploads")

            storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
            tus_server = TusServer(storage=storage, base_path="/files")
            assert tus_server is not None

            class Handler(TusHTTPRequestHandler):
                pass

            Handler.tus_server = tus_server
            assert Handler.tus_server is not None
        finally:
            import shutil

            shutil.rmtree(tmpdir)

    def test_server_startup(self):
        """Test 4: Start HTTP server."""
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "test.db")
            upload_dir = os.path.join(tmpdir, "uploads")

            storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
            tus_server = TusServer(storage=storage, base_path="/files")

            class Handler(TusHTTPRequestHandler):
                pass

            Handler.tus_server = tus_server

            server = HTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.5)

            assert port > 0
            server.shutdown()
        finally:
            import shutil

            shutil.rmtree(tmpdir)

    def test_file_upload(self):
        """Test 5: Test file upload."""
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "test.db")
            upload_dir = os.path.join(tmpdir, "uploads")

            storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
            tus_server = TusServer(storage=storage, base_path="/files")

            class Handler(TusHTTPRequestHandler):
                pass

            Handler.tus_server = tus_server

            server = HTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.5)

            client = TusClient(f"http://127.0.0.1:{port}/files", chunk_size=512)

            # Create test file
            test_file = os.path.join(tmpdir, "test.bin")
            test_data = (
                b"Python "
                + str(sys.version_info.major).encode()
                + b"."
                + str(sys.version_info.minor).encode()
                + b" test data\n" * 50
            )
            with open(test_file, "wb") as f:
                f.write(test_data)

            # Upload file
            upload_url = client.upload_file(test_file, metadata={"filename": "test.bin"})
            assert upload_url is not None

            # Verify data integrity
            upload_id = upload_url.split("/")[-1]
            uploaded_data = storage.read_file(upload_id)
            assert uploaded_data == test_data

            server.shutdown()
        finally:
            import shutil

            shutil.rmtree(tmpdir)

    def test_resume_functionality(self):
        """Test 7: Test resume capability."""
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "test.db")
            upload_dir = os.path.join(tmpdir, "uploads")

            storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
            tus_server = TusServer(storage=storage, base_path="/files")

            class Handler(TusHTTPRequestHandler):
                pass

            Handler.tus_server = tus_server

            server = HTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.5)

            client = TusClient(f"http://127.0.0.1:{port}/files", chunk_size=512)

            test_file2 = os.path.join(tmpdir, "test2.bin")
            with open(test_file2, "wb") as f:
                f.write(b"Resume test\n" * 20)

            # Create upload
            metadata = {"filename": "test2.bin"}
            upload_url2 = client._create_upload(os.path.getsize(test_file2), metadata)

            # Get upload info (should be at offset 0)
            info = client.get_upload_info(upload_url2)
            assert info["offset"] == 0, f"Expected offset 0, got {info['offset']}"

            # Upload partial data using Uploader
            from resumable_upload.client.uploader import Uploader

            uploader = Uploader(
                url=upload_url2,
                file_path=test_file2,
                chunk_size=100,
                max_retries=0,  # Disable retry for this test
            )
            try:
                # Upload first chunk
                uploader.upload_chunk()

                # Check offset updated
                info = client.get_upload_info(upload_url2)
                assert info["offset"] == 100, f"Expected offset 100, got {info['offset']}"
            finally:
                uploader.close()

            server.shutdown()
        finally:
            import shutil

            shutil.rmtree(tmpdir)

    def test_upload_deletion(self):
        """Test 8: Test upload deletion."""
        tmpdir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(tmpdir, "test.db")
            upload_dir = os.path.join(tmpdir, "uploads")

            storage = SQLiteStorage(db_path=db_path, upload_dir=upload_dir)
            tus_server = TusServer(storage=storage, base_path="/files")

            class Handler(TusHTTPRequestHandler):
                pass

            Handler.tus_server = tus_server

            server = HTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            time.sleep(0.5)

            client = TusClient(f"http://127.0.0.1:{port}/files", chunk_size=512)

            # Create test file and upload
            test_file = os.path.join(tmpdir, "test.bin")
            test_data = b"Test data\n" * 10
            with open(test_file, "wb") as f:
                f.write(test_data)

            upload_url = client.upload_file(test_file, metadata={"filename": "test.bin"})
            upload_id = upload_url.split("/")[-1]

            # Delete upload
            client.delete_upload(upload_url)
            upload_info = storage.get_upload(upload_id)
            assert upload_info is None, "Upload should be deleted"

            server.shutdown()
        finally:
            import shutil

            shutil.rmtree(tmpdir)
