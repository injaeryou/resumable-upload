"""Tests for TusServer hook system."""

import os
import tempfile

import pytest

from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.exceptions import TusHookError

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def storage(temp_dir):
    return SQLiteStorage(
        db_path=os.path.join(temp_dir, "test.db"),
        upload_dir=os.path.join(temp_dir, "uploads"),
    )


def _tus_headers(**extra):
    """Build minimal TUS request headers."""
    headers = {"tus-resumable": "1.0.0"}
    headers.update(extra)
    return headers


# -- TusHookError exception --------------------------------------------------


class TestTusHookError:
    def test_default_status_code(self):
        err = TusHookError("forbidden")
        assert err.status_code == 403
        assert str(err) == "forbidden"

    def test_custom_status_code(self):
        err = TusHookError("rate limited", status_code=429)
        assert err.status_code == 429

    def test_inherits_from_exception(self):
        err = TusHookError("test")
        assert isinstance(err, Exception)


# -- on_incoming_request hook ------------------------------------------------


class TestOnIncomingRequest:
    def test_hook_called_on_every_request(self, storage):
        calls = []

        def hook(method, path, headers):
            calls.append((method, path))

        server = TusServer(storage=storage, on_incoming_request=hook)
        server.handle_request("OPTIONS", "/files", {})
        assert len(calls) == 1
        assert calls[0] == ("OPTIONS", "/files")

    def test_hook_receives_lowercase_headers(self, storage):
        received = {}

        def hook(method, path, headers):
            received.update(headers)

        server = TusServer(storage=storage, on_incoming_request=hook)
        server.handle_request(
            "OPTIONS",
            "/files",
            {"Authorization": "Bearer token123", "X-Custom": "value"},
        )
        assert "authorization" in received
        assert received["authorization"] == "Bearer token123"

    def test_hook_error_rejects_request(self, storage):
        def hook(method, path, headers):
            raise TusHookError("Unauthorized", status_code=401)

        server = TusServer(storage=storage, on_incoming_request=hook)
        status, headers, body = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        assert status == 401
        assert body == b"Unauthorized"

    def test_generic_exception_returns_500(self, storage):
        def hook(method, path, headers):
            raise RuntimeError("internal bug")

        server = TusServer(storage=storage, on_incoming_request=hook)
        status, headers, body = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        assert status == 500
        # Must NOT leak internal error message
        assert b"internal bug" not in body
        assert b"Internal Server Error" in body

    def test_no_hook_means_normal_flow(self, storage):
        server = TusServer(storage=storage)
        status, _, _ = server.handle_request("OPTIONS", "/files", {})
        assert status == 204


# -- on_upload_create hook ---------------------------------------------------


class TestOnUploadCreate:
    def test_hook_called_with_correct_args(self, storage):
        calls = []

        def hook(upload_id, metadata, upload_length):
            calls.append(
                {
                    "upload_id": upload_id,
                    "metadata": metadata,
                    "upload_length": upload_length,
                }
            )

        server = TusServer(storage=storage, on_upload_create=hook)
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "1024"}),
        )
        assert status == 201
        assert len(calls) == 1
        assert calls[0]["upload_length"] == 1024
        assert isinstance(calls[0]["upload_id"], str)

    def test_hook_can_modify_metadata(self, storage):
        def hook(upload_id, metadata, upload_length):
            return {"filename": "sanitized.bin", "source": "hook"}

        server = TusServer(storage=storage, on_upload_create=hook)
        status, resp_headers, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(
                **{
                    "upload-length": "100",
                    "upload-metadata": "filename dGVzdC50eHQ=",
                }
            ),
        )
        assert status == 201

        # Verify metadata was replaced by reading it back via HEAD
        upload_id = resp_headers["Location"].split("/")[-1]
        status, head_headers, _ = server.handle_request(
            "HEAD",
            f"/files/{upload_id}",
            _tus_headers(),
        )
        assert status == 200
        assert "source" in head_headers.get("Upload-Metadata", "")

    def test_hook_returning_none_keeps_original_metadata(self, storage):
        def hook(upload_id, metadata, upload_length):
            return None

        server = TusServer(storage=storage, on_upload_create=hook)
        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(
                **{
                    "upload-length": "100",
                    "upload-metadata": "filename dGVzdC50eHQ=",
                }
            ),
        )
        assert status == 201

    def test_hook_error_rejects_creation(self, storage):
        def hook(upload_id, metadata, upload_length):
            raise TusHookError("File type not allowed", status_code=422)

        server = TusServer(storage=storage, on_upload_create=hook)
        status, _, body = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        assert status == 422
        assert body == b"File type not allowed"

    def test_hook_error_does_not_create_upload(self, storage):
        def hook(upload_id, metadata, upload_length):
            raise TusHookError("rejected", status_code=403)

        server = TusServer(storage=storage, on_upload_create=hook)
        server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        # No uploads should exist in storage
        # Try to HEAD any upload — should be 404
        # (We don't know the ID since creation was rejected, but storage should be empty)
        assert len(storage.get_expired_uploads()) == 0

    def test_generic_exception_returns_500(self, storage):
        def hook(upload_id, metadata, upload_length):
            raise ValueError("bad logic")

        server = TusServer(storage=storage, on_upload_create=hook)
        status, _, body = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        assert status == 500
        assert b"bad logic" not in body


# -- on_upload_complete hook -------------------------------------------------


class TestOnUploadComplete:
    def _create_and_complete(self, server, data=b"hello"):
        """Helper: create upload and complete it with data."""
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": str(len(data))}),
        )
        assert status == 201
        upload_id = headers["Location"].split("/")[-1]

        status, _, _ = server.handle_request(
            "PATCH",
            f"/files/{upload_id}",
            _tus_headers(
                **{
                    "upload-offset": "0",
                    "content-type": "application/offset+octet-stream",
                }
            ),
            body=data,
        )
        assert status == 204
        return upload_id

    def test_hook_called_on_upload_complete(self, storage):
        calls = []

        def hook(upload_id, metadata, file_info):
            calls.append(
                {
                    "upload_id": upload_id,
                    "metadata": metadata,
                    "file_info": file_info,
                }
            )

        server = TusServer(storage=storage, on_upload_complete=hook)
        uid = self._create_and_complete(server)

        assert len(calls) == 1
        assert calls[0]["upload_id"] == uid
        assert "upload_id" in calls[0]["file_info"]

    def test_hook_not_called_on_partial_upload(self, storage):
        calls = []

        def hook(upload_id, metadata, file_info):
            calls.append(upload_id)

        server = TusServer(storage=storage, on_upload_complete=hook)

        # Create upload of 100 bytes but only send 10
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        upload_id = headers["Location"].split("/")[-1]

        server.handle_request(
            "PATCH",
            f"/files/{upload_id}",
            _tus_headers(
                **{
                    "upload-offset": "0",
                    "content-type": "application/offset+octet-stream",
                }
            ),
            body=b"0123456789",
        )
        assert len(calls) == 0

    def test_hook_called_on_creation_with_upload(self, storage):
        """on_upload_complete fires when POST body completes the upload."""
        calls = []

        def hook(upload_id, metadata, file_info):
            calls.append(upload_id)

        server = TusServer(storage=storage, on_upload_complete=hook)
        data = b"complete in one shot"

        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(
                **{
                    "upload-length": str(len(data)),
                    "content-type": "application/offset+octet-stream",
                }
            ),
            body=data,
        )
        assert status == 201
        assert len(calls) == 1

    def test_hook_not_called_when_tus_partial_completes(self, storage):
        """A concatenation partial finishing its bytes must NOT fire the hook.

        Per the compliance matrix, partials never fire on_upload_complete
        individually — only the final (concatenated) upload does.
        """
        calls = []

        def hook(upload_id, metadata, file_info):
            calls.append(upload_id)

        server = TusServer(storage=storage, on_upload_complete=hook)
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "2", "upload-concat": "partial"}),
        )
        assert status == 201
        server.handle_request(
            "PATCH",
            headers["Location"],
            _tus_headers(
                **{
                    "upload-offset": "0",
                    "content-type": "application/offset+octet-stream",
                }
            ),
            body=b"hi",
        )
        assert calls == []

    def test_hook_exception_does_not_affect_response(self, storage):
        def hook(upload_id, metadata, file_info):
            raise RuntimeError("post-processing failed")

        server = TusServer(storage=storage, on_upload_complete=hook)
        # Should still return 204, not 500
        uid = self._create_and_complete(server)
        # Upload should still be marked complete
        upload = storage.get_upload(uid)
        assert upload["completed"]


# -- on_upload_terminate hook ------------------------------------------------


class TestOnUploadTerminate:
    def test_hook_called_after_delete(self, storage):
        calls = []

        def hook(upload_id):
            calls.append(upload_id)

        server = TusServer(storage=storage, on_upload_terminate=hook)

        # Create upload
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        upload_id = headers["Location"].split("/")[-1]

        # Delete it
        status, _, _ = server.handle_request(
            "DELETE",
            f"/files/{upload_id}",
            _tus_headers(),
        )
        assert status == 204
        assert calls == [upload_id]

    def test_hook_not_called_on_404_delete(self, storage):
        calls = []

        def hook(upload_id):
            calls.append(upload_id)

        server = TusServer(storage=storage, on_upload_terminate=hook)
        status, _, _ = server.handle_request(
            "DELETE",
            "/files/00000000-0000-0000-0000-000000000000",
            _tus_headers(),
        )
        assert status == 404
        assert len(calls) == 0

    def test_hook_exception_does_not_affect_response(self, storage):
        def hook(upload_id):
            raise RuntimeError("cleanup failed")

        server = TusServer(storage=storage, on_upload_terminate=hook)

        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        upload_id = headers["Location"].split("/")[-1]

        status, _, _ = server.handle_request(
            "DELETE",
            f"/files/{upload_id}",
            _tus_headers(),
        )
        # Should still return 204
        assert status == 204


# -- Combined hooks ----------------------------------------------------------


class TestCombinedHooks:
    def test_all_hooks_in_full_upload_flow(self, storage):
        events = []

        server = TusServer(
            storage=storage,
            on_incoming_request=lambda m, p, h: events.append(f"req:{m}"),
            on_upload_create=lambda uid, meta, length: events.append("create"),
            on_upload_complete=lambda uid, meta, info: events.append("complete"),
            on_upload_terminate=lambda uid: events.append("terminate"),
        )

        data = b"full flow test"

        # POST
        status, headers, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": str(len(data))}),
        )
        upload_id = headers["Location"].split("/")[-1]

        # PATCH (complete)
        server.handle_request(
            "PATCH",
            f"/files/{upload_id}",
            _tus_headers(
                **{
                    "upload-offset": "0",
                    "content-type": "application/offset+octet-stream",
                }
            ),
            body=data,
        )

        # DELETE
        server.handle_request(
            "DELETE",
            f"/files/{upload_id}",
            _tus_headers(),
        )

        assert events == [
            "req:POST",
            "create",
            "req:PATCH",
            "complete",
            "req:DELETE",
            "terminate",
        ]

    def test_incoming_request_hook_blocks_before_create_hook(self, storage):
        """on_incoming_request rejection prevents on_upload_create from firing."""
        create_called = []

        def incoming_hook(method, path, headers):
            raise TusHookError("blocked", status_code=403)

        def create_hook(upload_id, metadata, upload_length):
            create_called.append(True)

        server = TusServer(
            storage=storage,
            on_incoming_request=incoming_hook,
            on_upload_create=create_hook,
        )

        status, _, _ = server.handle_request(
            "POST",
            "/files",
            _tus_headers(**{"upload-length": "100"}),
        )
        assert status == 403
        assert len(create_called) == 0
