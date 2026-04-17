"""Tests for alternative fingerprint strategies."""

from __future__ import annotations

from io import BytesIO

from resumable_upload.fingerprint import (
    CallableFingerprint,
    Fingerprint,
    PartialMD5Fingerprint,
)


class TestFullSHA256Fingerprint:
    def test_deterministic_for_same_bytes(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        a = Fingerprint().get_fingerprint(str(f))
        b = Fingerprint().get_fingerprint(str(f))
        assert a == b
        assert a.startswith("size:5--sha256:")

    def test_different_content_gives_different_fingerprint(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"hello")
        f2.write_bytes(b"world")
        assert Fingerprint().get_fingerprint(str(f1)) != Fingerprint().get_fingerprint(str(f2))


class TestPartialMD5Fingerprint:
    def test_matches_on_identical_head_and_size(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"X" * 1000)
        f2.write_bytes(b"X" * 1000)
        strat = PartialMD5Fingerprint(probe_bytes=64)
        assert strat.get_fingerprint(str(f1)) == strat.get_fingerprint(str(f2))

    def test_differs_on_different_size(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"X" * 1000)
        f2.write_bytes(b"X" * 1001)
        strat = PartialMD5Fingerprint(probe_bytes=64)
        assert strat.get_fingerprint(str(f1)) != strat.get_fingerprint(str(f2))

    def test_differs_on_different_head_bytes(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"A" * 1000)
        f2.write_bytes(b"B" * 1000)
        strat = PartialMD5Fingerprint(probe_bytes=64)
        assert strat.get_fingerprint(str(f1)) != strat.get_fingerprint(str(f2))

    def test_fingerprint_shape(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        fp = PartialMD5Fingerprint(probe_bytes=64).get_fingerprint(str(f))
        # "size:<n>--md5:<hex>" — matches tus-py-client's scheme
        assert fp.startswith("size:5--md5:")

    def test_stream_and_path_match(self, tmp_path):
        data = b"Y" * 2000
        f = tmp_path / "c.bin"
        f.write_bytes(data)
        strat = PartialMD5Fingerprint(probe_bytes=128)
        from_path = strat.get_fingerprint(str(f))
        from_stream = strat.get_fingerprint(BytesIO(data))
        assert from_path == from_stream

    def test_stream_position_preserved(self, tmp_path):
        data = b"Z" * 100
        stream = BytesIO(data)
        stream.seek(37)
        PartialMD5Fingerprint(probe_bytes=32).get_fingerprint(stream)
        assert stream.tell() == 37


class TestCallableFingerprint:
    def test_delegates_to_callable(self):
        strat = CallableFingerprint(lambda _src: "constant-key")
        assert strat.get_fingerprint("any-path") == "constant-key"

    def test_callable_receives_source(self):
        seen: list = []

        def cap(src):
            seen.append(src)
            return f"fake:{src}"

        strat = CallableFingerprint(cap)
        assert strat.get_fingerprint("hello") == "fake:hello"
        assert seen == ["hello"]
