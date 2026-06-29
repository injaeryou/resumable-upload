"""Unit tests for the read-on-demand FileSlice used by parallel uploads."""

import io

from resumable_upload.client._fileslice import FileSlice


def _write(tmp_path, data: bytes):
    p = tmp_path / "data.bin"
    p.write_bytes(data)
    return str(p)


def test_reads_only_its_range(tmp_path):
    path = _write(tmp_path, b"0123456789")
    fs = FileSlice(path, lo=3, length=4)  # bytes "3456"
    try:
        assert fs.read() == b"3456"
        assert fs.read() == b""  # past end of slice, not into neighbor bytes
    finally:
        fs.close()


def test_chunked_read_and_tell(tmp_path):
    path = _write(tmp_path, b"abcdefghij")
    fs = FileSlice(path, lo=2, length=6)  # "cdefgh"
    try:
        assert fs.read(2) == b"cd"
        assert fs.tell() == 2
        assert fs.read(2) == b"ef"
        assert fs.read(99) == b"gh"  # clamped to slice end
        assert fs.tell() == 6
    finally:
        fs.close()


def test_seek_modes_and_clamping(tmp_path):
    path = _write(tmp_path, b"0123456789")
    fs = FileSlice(path, lo=1, length=5)  # "12345"
    try:
        assert fs.seek(0, io.SEEK_END) == 5
        assert fs.read() == b""
        assert fs.seek(2) == 2 and fs.read() == b"345"
        fs.seek(1)
        assert fs.seek(2, io.SEEK_CUR) == 3 and fs.read() == b"45"
        assert fs.seek(-100) == 0  # clamped to start
        assert fs.seek(100) == 5  # clamped to end
    finally:
        fs.close()


def test_readinto(tmp_path):
    path = _write(tmp_path, b"helloworld")
    fs = FileSlice(path, lo=5, length=5)  # "world"
    try:
        buf = bytearray(3)
        assert fs.readinto(buf) == 3
        assert bytes(buf) == b"wor"
        assert fs.readinto(buf) == 2  # only "ld" left
        assert bytes(buf[:2]) == b"ld"
        assert fs.readinto(buf) == 0  # exhausted
    finally:
        fs.close()


def test_close_releases_descriptor(tmp_path):
    path = _write(tmp_path, b"abc")
    fs = FileSlice(path, lo=0, length=3)
    fs.close()
    assert fs._f.closed
    fs.close()  # idempotent
