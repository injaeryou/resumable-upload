# tests/test_protocol_helpers.py
import base64
import hashlib

import pytest

from resumable_upload.client import _protocol as P


def test_encode_metadata_b64_pairs():
    out = P.encode_metadata({"filename": "a.bin"}, "utf-8")
    assert out == [f"filename {base64.b64encode(b'a.bin').decode()}"]


def test_encode_metadata_rejects_space_or_comma_key():
    with pytest.raises(ValueError, match="cannot be empty nor contain"):
        P.encode_metadata({"bad key": "x"}, "utf-8")


def test_parse_upload_metadata_roundtrip():
    enc = base64.b64encode(b"a.bin").decode()
    assert P.parse_upload_metadata(f"filename {enc}", "utf-8") == {"filename": "a.bin"}
    assert P.parse_upload_metadata(None, "utf-8") == {}


def test_parse_upload_info_complete_flag():
    info = P.parse_upload_info("10", "10", None, "utf-8")
    assert info == {"offset": 10, "length": 10, "complete": True, "metadata": {}}
    assert P.parse_upload_info("0", "10", None, "utf-8")["complete"] is False
    # A real zero-length upload (Upload-Length "0") is complete at offset 0.
    assert P.parse_upload_info("0", "0", None, "utf-8")["complete"] is True
    # A deferred upload has no Upload-Length header (None) — never complete,
    # even though its offset may be 0.
    assert P.parse_upload_info("0", None, None, "utf-8")["complete"] is False


def test_parse_server_info_extensions_and_max_size():
    info = P.parse_server_info("1.0.0", "creation,concatenation", "1024", "1.0.0")
    assert info["version"] == "1.0.0"
    assert info["extensions"] == ["creation", "concatenation"]
    assert info["max_size"] == 1024
    assert P.parse_server_info(None, "", None, "1.0.0")["max_size"] is None


def test_parse_server_info_empty_version_header_stays_empty():
    # Present-but-empty header must NOT fall back to the default.
    assert P.parse_server_info("", "creation", None, "1.0.0")["version"] == ""
    # Absent (None) header falls back.
    assert P.parse_server_info(None, "creation", None, "1.0.0")["version"] == "1.0.0"


def test_resolve_checksum_algorithm():
    assert P.resolve_checksum_algorithm(True) == "sha1"
    assert P.resolve_checksum_algorithm(False) is None
    assert P.resolve_checksum_algorithm(None) is None
    assert P.resolve_checksum_algorithm("SHA256") == "sha256"


def test_checksum_header_matches_server_format():
    h = P.checksum_header("sha1", b"hello")
    assert h == "sha1 " + base64.b64encode(hashlib.sha1(b"hello").digest()).decode()


def test_retry_delay_exponential_capped():
    assert P.retry_delay(1.0, 0) == 1.0
    assert P.retry_delay(1.0, 1) == 2.0
    assert P.retry_delay(1.0, 100) == 60.0


def test_split_boundaries_last_slice_absorbs_remainder():
    assert P.split_boundaries(10, 3) == [(0, 3), (3, 6), (6, 10)]
    assert P.split_boundaries(2, 5) == [(0, 2)]  # skip empty slices
    assert P.split_boundaries(0, 3) == []
