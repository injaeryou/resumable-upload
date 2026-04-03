"""Test suite for server module."""

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage

# A valid UUID used across tests that need a real UUID format
_VALID_UUID = "12345678-1234-5678-1234-123456789abc"
# A valid UUID that will never exist in storage
_NONEXISTENT_UUID = "00000000-0000-0000-0000-000000000000"


class TestTusServer:
    """Tests for TusServer."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def storage(self, temp_dir):
        """Create a storage instance for tests."""
        db_path = os.path.join(temp_dir, "test.db")
        upload_dir = os.path.join(temp_dir, "uploads")
        return SQLiteStorage(db_path=db_path, upload_dir=upload_dir)

    @pytest.fixture
    def server(self, storage):
        """Create a server instance for tests."""
        return TusServer(storage=storage, base_path="/files")

    def test_handle_options(self, server):
        """Test OPTIONS request."""
        status, headers, body = server.handle_request("OPTIONS", "/files", {})

        assert status == 204
        assert headers["Tus-Resumable"] == "1.0.0"
        assert headers["Tus-Version"] == "1.0.0"
        assert "creation" in headers["Tus-Extension"]

    def test_handle_create(self, server):
        """Test POST request to create upload."""
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "1024",
            "upload-metadata": "filename dGVzdC50eHQ=",  # base64("test.txt")
        }

        status, response_headers, body = server.handle_request("POST", "/files", headers)

        assert status == 201
        assert "Location" in response_headers
        assert response_headers["Upload-Offset"] == "0"

    def test_handle_create_without_length(self, server):
        """Test POST request without Upload-Length."""
        headers = {
            "tus-resumable": "1.0.0",
        }

        status, response_headers, body = server.handle_request("POST", "/files", headers)

        assert status == 400

    def test_handle_head(self, server, storage):
        """Test HEAD request to get upload info."""
        # Create upload with a valid UUID
        upload_id = _VALID_UUID
        storage.create_upload(upload_id, 1024, {"filename": "test.txt"})

        headers = {"tus-resumable": "1.0.0"}

        status, response_headers, body = server.handle_request(
            "HEAD", f"/files/{upload_id}", headers
        )

        assert status == 200
        assert response_headers["Upload-Offset"] == "0"
        assert response_headers["Upload-Length"] == "1024"

    def test_handle_head_not_found(self, server):
        """Test HEAD request for non-existent upload (with valid UUID)."""
        headers = {"tus-resumable": "1.0.0"}

        status, response_headers, body = server.handle_request(
            "HEAD", f"/files/{_NONEXISTENT_UUID}", headers
        )

        assert status == 404

    def test_handle_patch(self, server, storage):
        """Test PATCH request to upload data."""
        # Create upload with a valid UUID
        upload_id = _VALID_UUID
        storage.create_upload(upload_id, 100, {"filename": "test.txt"})

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
        }

        data = b"Hello World!"

        status, response_headers, body = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, data
        )

        assert status == 204
        assert response_headers["Upload-Offset"] == str(len(data))

        # Verify data was written
        uploaded_data = storage.read_file(upload_id)
        assert uploaded_data.startswith(data)

    def test_handle_patch_offset_mismatch(self, server, storage):
        """Test PATCH request with offset mismatch."""
        upload_id = _VALID_UUID
        storage.create_upload(upload_id, 100, {})

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "50",  # Wrong offset
            "content-type": "application/offset+octet-stream",
        }

        status, response_headers, body = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, b"data"
        )

        assert status == 409

    def test_handle_patch_invalid_content_type(self, server, storage):
        """Test PATCH request with invalid content type."""
        upload_id = _VALID_UUID
        storage.create_upload(upload_id, 100, {})

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "text/plain",  # Wrong content type
        }

        status, response_headers, body = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, b"data"
        )

        assert status == 415

    def test_handle_delete(self, server, storage):
        """Test DELETE request to terminate upload."""
        upload_id = _VALID_UUID
        storage.create_upload(upload_id, 100, {})

        headers = {"tus-resumable": "1.0.0"}

        status, response_headers, body = server.handle_request(
            "DELETE", f"/files/{upload_id}", headers
        )

        assert status == 204
        assert storage.get_upload(upload_id) is None

    def test_handle_delete_not_found(self, server):
        """Test DELETE request for non-existent upload (with valid UUID)."""
        headers = {"tus-resumable": "1.0.0"}

        status, response_headers, body = server.handle_request(
            "DELETE", f"/files/{_NONEXISTENT_UUID}", headers
        )

        assert status == 404

    def test_invalid_tus_version(self, server):
        """Test request with invalid TUS version."""
        headers = {"tus-resumable": "0.9.0"}

        status, response_headers, body = server.handle_request("POST", "/files", headers)

        assert status == 412

    def test_max_size_limit(self, storage):
        """Test upload size limit."""
        server = TusServer(storage=storage, max_size=1000)

        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "2000",
        }

        status, response_headers, body = server.handle_request("POST", "/files", headers)

        assert status == 413

    # --- Phase 1.2: Path Traversal tests ---

    def test_invalid_upload_id_format_returns_400(self, server):
        """Non-UUID upload_id is rejected with 400."""
        headers = {"tus-resumable": "1.0.0"}
        for bad_id in ["../etc/passwd", "nonexistent", "../../secret", "foo bar"]:
            status, resp_headers, body = server.handle_request("HEAD", f"/files/{bad_id}", headers)
            assert status == 400, f"Expected 400 for id={bad_id!r}, got {status}"
            assert "Tus-Resumable" in resp_headers

    def test_path_traversal_rejected(self, server):
        """Path traversal attempts return 400."""
        headers = {"tus-resumable": "1.0.0"}
        for method in ("HEAD", "PATCH", "DELETE"):
            status, resp_headers, body = server.handle_request(
                method, "/files/../etc/passwd", headers
            )
            assert status == 400

    def test_valid_uuid_passes_to_storage(self, server, storage):
        """Valid UUID is forwarded to storage (not rejected at routing)."""
        upload_id = str(uuid.uuid4())
        storage.create_upload(upload_id, 50, {})

        headers = {"tus-resumable": "1.0.0"}
        status, resp_headers, body = server.handle_request("HEAD", f"/files/{upload_id}", headers)
        assert status == 200

    # --- Phase 1.3: Completed upload PATCH ---

    def test_patch_on_completed_upload_returns_403(self, server, storage):
        """PATCH on a completed upload returns 403 Forbidden."""
        upload_id = str(uuid.uuid4())
        data = b"complete data"
        storage.create_upload(upload_id, len(data), {})
        storage.write_chunk(upload_id, 0, data)
        storage.update_offset(upload_id, len(data))  # marks completed=True

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": str(len(data)),
            "content-type": "application/offset+octet-stream",
        }
        status, resp_headers, body = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, b"more"
        )
        assert status == 403
        assert "Tus-Resumable" in resp_headers

    # --- Phase 1.4: Tus-Resumable in all error responses ---

    def test_error_400_includes_tus_resumable(self, server):
        """400 error response includes Tus-Resumable header."""
        headers = {"tus-resumable": "1.0.0"}
        # Trigger 400 via missing Upload-Length
        status, resp_headers, body = server.handle_request("POST", "/files", headers)
        assert status == 400
        assert resp_headers.get("Tus-Resumable") == "1.0.0"

    def test_error_404_includes_tus_resumable(self, server):
        """404 error response includes Tus-Resumable header."""
        headers = {"tus-resumable": "1.0.0"}
        status, resp_headers, body = server.handle_request(
            "HEAD", f"/files/{_NONEXISTENT_UUID}", headers
        )
        assert status == 404
        assert resp_headers.get("Tus-Resumable") == "1.0.0"

    def test_error_409_includes_tus_resumable(self, server, storage):
        """409 error response includes Tus-Resumable header."""
        upload_id = str(uuid.uuid4())
        storage.create_upload(upload_id, 100, {})

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "50",
            "content-type": "application/offset+octet-stream",
        }
        status, resp_headers, body = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, b"data"
        )
        assert status == 409
        assert resp_headers.get("Tus-Resumable") == "1.0.0"

    def test_error_413_includes_tus_resumable(self, storage):
        """413 error response includes Tus-Resumable header."""
        server = TusServer(storage=storage, max_size=1000)
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "2000",
        }
        status, resp_headers, body = server.handle_request("POST", "/files", headers)
        assert status == 413
        assert resp_headers.get("Tus-Resumable") == "1.0.0"

    # --- Phase 2.1: Expiration extension ---

    def test_expiration_header_in_create_response(self, storage):
        """Upload-Expires is present in POST response when upload_expiry set."""
        server = TusServer(storage=storage, upload_expiry=3600)
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "1024",
        }
        status, resp_headers, body = server.handle_request("POST", "/files", headers)
        assert status == 201
        assert "Upload-Expires" in resp_headers

    def test_expiration_header_in_head_response(self, storage):
        """Upload-Expires is present in HEAD response when expires_at is set."""
        server = TusServer(storage=storage, upload_expiry=3600)
        # Create upload via server so expires_at is stored
        create_headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "1024",
        }
        status, resp_headers, body = server.handle_request("POST", "/files", create_headers)
        assert status == 201
        upload_url = resp_headers["Location"]
        upload_id = upload_url.split("/")[-1]

        head_headers = {"tus-resumable": "1.0.0"}
        status, resp_headers, body = server.handle_request(
            "HEAD", f"/files/{upload_id}", head_headers
        )
        assert status == 200
        assert "Upload-Expires" in resp_headers

    def test_expired_upload_patch_returns_410(self, storage):
        """PATCH on expired upload returns 410 Gone."""
        server = TusServer(storage=storage, upload_expiry=3600)
        upload_id = str(uuid.uuid4())
        # Create with a past expiry
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        storage.create_upload(upload_id, 100, {}, expires_at=past)

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
        }
        status, resp_headers, body = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, b"data"
        )
        assert status == 410
        assert resp_headers.get("Tus-Resumable") == "1.0.0"

    def test_expiration_in_supported_extensions(self, server):
        """expiration is listed in Tus-Extension."""
        status, headers, body = server.handle_request("OPTIONS", "/files", {})
        assert "expiration" in headers["Tus-Extension"]

    # --- Phase 2.2: creation-with-upload ---

    def test_creation_with_upload_data_in_post(self, server):
        """POST with body and correct Content-Type processes initial data."""
        initial_data = b"initial chunk"
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "100",
            "content-type": "application/offset+octet-stream",
        }
        status, resp_headers, body = server.handle_request("POST", "/files", headers, initial_data)
        assert status == 201
        assert resp_headers["Upload-Offset"] == str(len(initial_data))

    def test_creation_with_upload_offset_updated(self, server, storage):
        """creation-with-upload: stored offset matches initial data length."""
        initial_data = b"hello world"
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "100",
            "content-type": "application/offset+octet-stream",
        }
        status, resp_headers, body = server.handle_request("POST", "/files", headers, initial_data)
        assert status == 201
        upload_id = resp_headers["Location"].split("/")[-1]
        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["offset"] == len(initial_data)

    def test_creation_with_upload_in_supported_extensions(self, server):
        """creation-with-upload is listed in Tus-Extension."""
        status, headers, body = server.handle_request("OPTIONS", "/files", {})
        assert "creation-with-upload" in headers["Tus-Extension"]

    # --- Phase 2.3: CORS ---

    def test_cors_headers_not_present_by_default(self, server):
        """No CORS headers when cors_allow_origins is None."""
        status, headers, body = server.handle_request("OPTIONS", "/files", {})
        assert "Access-Control-Allow-Origin" not in headers

    def test_cors_headers_in_options_when_enabled(self, storage):
        """CORS headers included in OPTIONS when cors_allow_origins is set."""
        server = TusServer(storage=storage, cors_allow_origins="*")
        status, headers, body = server.handle_request("OPTIONS", "/files", {})
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert "Access-Control-Allow-Methods" in headers
        assert "Access-Control-Allow-Headers" in headers

    def test_cors_headers_in_all_responses_when_enabled(self, storage):
        """CORS headers are added to all responses when cors_allow_origins is set."""
        server = TusServer(storage=storage, cors_allow_origins="https://example.com")

        # POST (creates upload)
        post_headers = {"tus-resumable": "1.0.0", "upload-length": "100"}
        status, resp_headers, body = server.handle_request("POST", "/files", post_headers)
        assert resp_headers.get("Access-Control-Allow-Origin") == "https://example.com"

        # Error response (400)
        status, resp_headers, body = server.handle_request(
            "HEAD", "/files/invalid-id", {"tus-resumable": "1.0.0"}
        )
        assert status == 400
        assert resp_headers.get("Access-Control-Allow-Origin") == "https://example.com"

    # --- Review fixes: server validation ---

    def test_patch_exceeding_upload_length_returns_400(self, server):
        """PATCH that would exceed declared upload length is rejected with 400."""
        upload_id = str(uuid.uuid4())
        server.storage.create_upload(upload_id, 100, {})

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
        }
        # Send 200 bytes when upload_length is 100
        status, _, _ = server.handle_request("PATCH", f"/files/{upload_id}", headers, b"x" * 200)
        assert status == 400

    def test_patch_negative_offset_returns_400(self, server):
        """PATCH with negative Upload-Offset is rejected with 400."""
        upload_id = str(uuid.uuid4())
        server.storage.create_upload(upload_id, 100, {})

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "-1",
            "content-type": "application/offset+octet-stream",
        }
        status, _, _ = server.handle_request("PATCH", f"/files/{upload_id}", headers, b"data")
        assert status == 400

    def test_expired_uploads_cleaned_up_after_request(self, storage):
        """Expired uploads are removed after cleanup_interval has elapsed."""
        server = TusServer(storage=storage, upload_expiry=1, cleanup_interval=0)

        upload_id = str(uuid.uuid4())
        past = datetime.now(timezone.utc) - timedelta(seconds=2)
        storage.create_upload(upload_id, 100, {}, expires_at=past)

        # Any request triggers cleanup after processing
        server.handle_request("OPTIONS", "/files", {})

        # Expired upload should now be gone
        assert storage.get_upload(upload_id) is None

    def test_creation_with_upload_wrong_content_type_body_ignored(self, server):
        """POST with body but wrong Content-Type: body is ignored, upload created at offset 0."""
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "50",
            "content-type": "application/json",  # Wrong type — body ignored per TUS spec
        }
        status, resp_headers, _ = server.handle_request("POST", "/files", headers, b"x" * 10)
        assert status == 201
        assert resp_headers.get("Upload-Offset") == "0"  # Body was not processed

    # --- TUS spec compliance ---

    def test_options_includes_tus_checksum_algorithm(self, server):
        """OPTIONS response includes Tus-Checksum-Algorithm: sha1."""
        status, headers, _ = server.handle_request("OPTIONS", "/files", {})
        assert status == 204
        assert headers.get("Tus-Checksum-Algorithm") == "sha1"

    def test_patch_response_includes_upload_expires_when_expiry_set(self, storage):
        """PATCH response includes Upload-Expires header when upload has expiry."""
        server = TusServer(storage=storage, upload_expiry=3600)
        upload_id = str(uuid.uuid4())
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        storage.create_upload(upload_id, 100, {}, expires_at=future)

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
        }
        status, resp_headers, _ = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, b"data"
        )
        assert status == 204
        assert "Upload-Expires" in resp_headers

    def test_create_negative_upload_length_returns_400(self, server):
        """POST with negative Upload-Length is rejected with 400."""
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "-1",
        }
        status, _, _ = server.handle_request("POST", "/files", headers)
        assert status == 400

    def test_create_zero_length_upload_is_valid(self, server, storage):
        """POST with Upload-Length: 0 creates an immediately completed upload."""
        headers = {
            "tus-resumable": "1.0.0",
            "upload-length": "0",
        }
        status, resp_headers, _ = server.handle_request("POST", "/files", headers)
        assert status == 201
        assert resp_headers["Upload-Offset"] == "0"
        upload_id = resp_headers["Location"].split("/")[-1]
        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["upload_length"] == 0

    def test_patch_empty_body_is_valid(self, server, storage):
        """PATCH with empty body is valid and leaves offset unchanged."""
        upload_id = str(uuid.uuid4())
        storage.create_upload(upload_id, 100, {})

        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
        }
        status, resp_headers, _ = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, b""
        )
        assert status == 204
        assert resp_headers["Upload-Offset"] == "0"

    def test_checksum_mismatch_returns_460(self, server, storage):
        """PATCH with wrong Upload-Checksum returns 460."""
        import base64
        import hashlib

        upload_id = str(uuid.uuid4())
        storage.create_upload(upload_id, 100, {})

        data = b"hello"
        wrong_checksum = base64.b64encode(hashlib.sha1(b"wrong data").digest()).decode()
        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
            "upload-checksum": f"sha1 {wrong_checksum}",
        }
        status, _, _ = server.handle_request("PATCH", f"/files/{upload_id}", headers, data)
        assert status == 460

    def test_head_includes_cache_control_no_store(self, server, storage):
        """HEAD response includes Cache-Control: no-store."""
        upload_id = str(uuid.uuid4())
        storage.create_upload(upload_id, 100, {})

        headers = {"tus-resumable": "1.0.0"}
        status, resp_headers, _ = server.handle_request("HEAD", f"/files/{upload_id}", headers)
        assert status == 200
        assert resp_headers.get("Cache-Control") == "no-store"

    def test_expired_upload_head_returns_410(self, storage):
        """HEAD on expired upload returns 410 Gone."""
        server = TusServer(storage=storage, upload_expiry=3600)
        upload_id = str(uuid.uuid4())
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        storage.create_upload(upload_id, 100, {}, expires_at=past)

        status, resp_headers, _ = server.handle_request(
            "HEAD", f"/files/{upload_id}", {"tus-resumable": "1.0.0"}
        )
        assert status == 410
        assert resp_headers.get("Tus-Resumable") == "1.0.0"

    def test_checksum_correct_patch_succeeds(self, server, storage):
        """PATCH with correct Upload-Checksum succeeds."""
        import base64
        import hashlib

        upload_id = str(uuid.uuid4())
        storage.create_upload(upload_id, 100, {})

        data = b"hello world"
        checksum = base64.b64encode(hashlib.sha1(data).digest()).decode()
        headers = {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
            "upload-checksum": f"sha1 {checksum}",
        }
        status, resp_headers, _ = server.handle_request(
            "PATCH", f"/files/{upload_id}", headers, data
        )
        assert status == 204
        assert resp_headers["Upload-Offset"] == str(len(data))


def test_max_chunk_size_rejects_large_chunk():
    """Test that chunks exceeding max_chunk_size are rejected."""
    tmp = tempfile.mkdtemp()
    storage = SQLiteStorage(
        db_path=os.path.join(tmp, "test.db"), upload_dir=os.path.join(tmp, "uploads")
    )
    server = TusServer(storage=storage, max_chunk_size=512)

    # Create an upload
    status, headers, _ = server.handle_request(
        "POST",
        "/files",
        {"tus-resumable": "1.0.0", "upload-length": "2048"},
        b"",
    )
    assert status == 201
    upload_id = headers["Location"].split("/")[-1]

    # Try to send a chunk larger than max_chunk_size
    large_chunk = b"x" * 1024
    status, _, body = server.handle_request(
        "PATCH",
        f"/files/{upload_id}",
        {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
        },
        large_chunk,
    )
    assert status == 413
    assert b"Chunk exceeds maximum chunk size" in body

    # A small chunk should succeed
    small_chunk = b"x" * 256
    status, _, _ = server.handle_request(
        "PATCH",
        f"/files/{upload_id}",
        {
            "tus-resumable": "1.0.0",
            "upload-offset": "0",
            "content-type": "application/offset+octet-stream",
        },
        small_chunk,
    )
    assert status == 204


def test_zero_byte_upload_marked_completed():
    """Test that zero-byte uploads are correctly marked as completed in DB."""
    temp_dir = tempfile.mkdtemp()
    try:
        storage = SQLiteStorage(db_path=os.path.join(temp_dir, "test.db"), upload_dir=temp_dir)
        hook_called = []
        server = TusServer(
            storage=storage,
            on_upload_complete=lambda uid, meta, info: hook_called.append(uid),
        )

        # Create a zero-length upload
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            {"tus-resumable": "1.0.0", "upload-length": "0"},
            b"",
        )
        assert status == 201
        upload_id = headers["Location"].split("/")[-1]

        # Verify the upload is marked completed in storage
        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["completed"] is True
        assert upload["offset"] == 0
        assert upload["upload_length"] == 0

        # Verify on_upload_complete hook was called
        assert len(hook_called) == 1
        assert hook_called[0] == upload_id
    finally:
        shutil.rmtree(temp_dir)


def test_unsupported_checksum_algorithm():
    """Test that unsupported checksum algorithms are rejected with 400."""
    import base64
    import hashlib

    temp_dir = tempfile.mkdtemp()
    try:
        storage = SQLiteStorage(db_path=os.path.join(temp_dir, "test.db"), upload_dir=temp_dir)
        server = TusServer(storage=storage)

        # Create an upload
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            {"tus-resumable": "1.0.0", "upload-length": "5"},
            b"",
        )
        assert status == 201
        upload_id = headers["Location"].split("/")[-1]

        # Send a PATCH with an unsupported algorithm (md5)
        data = b"hello"
        md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()
        status, _, body = server.handle_request(
            "PATCH",
            f"/files/{upload_id}",
            {
                "tus-resumable": "1.0.0",
                "upload-offset": "0",
                "content-type": "application/offset+octet-stream",
                "upload-checksum": f"md5 {md5_hash}",
            },
            data,
        )
        assert status == 400
        assert b"Unsupported checksum algorithm" in body
    finally:
        shutil.rmtree(temp_dir)


def test_metadata_key_without_value():
    """Test that metadata keys without values are parsed correctly per TUS spec."""
    temp_dir = tempfile.mkdtemp()
    try:
        storage = SQLiteStorage(db_path=os.path.join(temp_dir, "test.db"), upload_dir=temp_dir)
        server = TusServer(storage=storage)

        # Create upload with a value-less metadata key
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            {
                "tus-resumable": "1.0.0",
                "upload-length": "0",
                "upload-metadata": "is-confidential,filename dGVzdA==",
            },
            b"",
        )
        assert status == 201
        upload_id = headers["Location"].split("/")[-1]

        # Verify metadata was parsed correctly
        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["metadata"]["is-confidential"] == ""
        assert upload["metadata"]["filename"] == "test"
    finally:
        shutil.rmtree(temp_dir)


def test_on_upload_create_hook_replaces_metadata():
    """Test that on_upload_create hook can replace metadata dict."""
    temp_dir = tempfile.mkdtemp()
    try:
        storage = SQLiteStorage(db_path=os.path.join(temp_dir, "test.db"), upload_dir=temp_dir)

        def modify_metadata(upload_id, metadata, upload_length):
            return {"original": "replaced", "added_by_hook": "yes"}

        server = TusServer(storage=storage, on_upload_create=modify_metadata)

        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            {
                "tus-resumable": "1.0.0",
                "upload-length": "10",
                "upload-metadata": "filename dGVzdA==",
            },
            b"",
        )
        assert status == 201
        upload_id = headers["Location"].split("/")[-1]

        upload = storage.get_upload(upload_id)
        assert upload is not None
        assert upload["metadata"] == {"original": "replaced", "added_by_hook": "yes"}
        assert "filename" not in upload["metadata"]
    finally:
        shutil.rmtree(temp_dir)
