# tests/test_protocol_helpers.py
import base64

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


def test_parse_server_info_extensions_and_max_size():
    info = P.parse_server_info("1.0.0", "creation,concatenation", "1024", "1.0.0")
    assert info["version"] == "1.0.0"
    assert info["extensions"] == ["creation", "concatenation"]
    assert info["max_size"] == 1024
    assert P.parse_server_info(None, "", None, "1.0.0")["max_size"] is None
