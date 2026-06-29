# resumable_upload/client/_fileslice.py
"""Read-on-demand view over a byte range of a file, shared by sync + async
parallel uploads so a slice is never loaded into memory all at once."""

from __future__ import annotations

import io


class FileSlice(io.RawIOBase):
    """A read-only, seekable file-like view over ``[lo, lo + length)`` of *path*.

    Positions are slice-relative (``0 .. length``). The uploader reads one
    ``chunk_size`` block at a time, so memory use stays at one chunk instead of
    the whole slice — the point of using this over ``io.BytesIO(f.read(length))``.

    Owns its own file descriptor; the caller must :meth:`close` it (the uploader
    treats an injected stream as not-owned and will not close it).
    """

    def __init__(self, path: str, lo: int, length: int) -> None:
        super().__init__()
        self._f = open(path, "rb")  # noqa: SIM115 — closed in close()
        self._lo = lo
        self._length = length
        self._pos = 0
        self._f.seek(lo)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            new = pos
        elif whence == io.SEEK_CUR:
            new = self._pos + pos
        elif whence == io.SEEK_END:
            new = self._length + pos
        else:
            raise ValueError(f"invalid whence: {whence}")
        self._pos = max(0, min(new, self._length))
        self._f.seek(self._lo + self._pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, b) -> int:
        remaining = self._length - self._pos
        if remaining <= 0:
            return 0
        n = min(len(b), remaining)
        data = self._f.read(n)
        b[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def close(self) -> None:
        try:
            if not self._f.closed:
                self._f.close()
        finally:
            super().close()
