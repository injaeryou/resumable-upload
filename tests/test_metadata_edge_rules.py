"""Upload-Metadata edge-rule compliance (TUS 1.0.0).

Spec: comma-separated `key [SP base64(value)]` pairs; keys MUST be unique,
MUST NOT be empty, MUST NOT contain spaces or commas, MUST be ASCII; the
value MAY be empty and the SP separator MAY then be omitted.

(An empty key is unrepresentable after the parser's whitespace stripping —
a bare `,,` pair is skipped — so no explicit empty-key test exists.)
"""

import base64
import os
import shutil
import tempfile

import pytest

from resumable_upload.server import TusServer
from resumable_upload.storage import SQLiteStorage


@pytest.fixture
def server():
    temp_dir = tempfile.mkdtemp()
    try:
        storage = SQLiteStorage(
            db_path=os.path.join(temp_dir, "u.db"),
            upload_dir=os.path.join(temp_dir, "files"),
        )
        yield TusServer(storage=storage, base_path="/files")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _post(server, metadata_header):
    return server.handle_request(
        "POST",
        "/files",
        {
            "Tus-Resumable": "1.0.0",
            "Upload-Length": "0",
            "Upload-Metadata": metadata_header,
        },
        b"",
    )


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


class TestKeyRules:
    def test_duplicate_keys_rejected(self, server):
        status, _, body = _post(server, f"filename {_b64('a')},filename {_b64('b')}")
        assert status == 400
        assert b"duplicate" in body.lower()

    def test_non_ascii_key_rejected(self, server):
        status, _, body = _post(server, f"파일명 {_b64('a')}")
        assert status == 400
        assert b"ascii" in body.lower()

    def test_bare_key_accepted(self, server):
        status, headers, _ = _post(server, "is-confidential")
        assert status == 201
        upload_id = headers["Location"].rsplit("/", 1)[1]
        assert server.storage.get_upload(upload_id)["metadata"] == {"is-confidential": ""}

    def test_bare_key_with_trailing_space_accepted(self, server):
        # Value MAY be empty with the SP separator still present.
        status, headers, _ = _post(server, "is-confidential ")
        assert status == 201
        upload_id = headers["Location"].rsplit("/", 1)[1]
        assert server.storage.get_upload(upload_id)["metadata"] == {"is-confidential": ""}

    def test_mixed_bare_and_valued_keys(self, server):
        status, headers, _ = _post(server, f"flag,filename {_b64('test.txt')}")
        assert status == 201
        upload_id = headers["Location"].rsplit("/", 1)[1]
        meta = server.storage.get_upload(upload_id)["metadata"]
        assert meta == {"flag": "", "filename": "test.txt"}

    def test_head_round_trips_metadata(self, server):
        status, headers, _ = _post(server, f"filename {_b64('한글.txt')}")
        assert status == 201
        location = headers["Location"]
        # Zero-length upload is complete immediately; HEAD must echo metadata.
        status, head_headers, _ = server.handle_request(
            "HEAD", location, {"Tus-Resumable": "1.0.0"}, b""
        )
        assert status == 200
        echoed = head_headers["Upload-Metadata"]
        assert f"filename {_b64('한글.txt')}" in echoed
