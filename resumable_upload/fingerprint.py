"""
File fingerprinting for unique identification of uploads.

Uses SHA-256 hash + file size to generate unique fingerprints for resumable uploads.
"""

import hashlib
import os
from typing import IO, Callable, Union

FileSource = Union[str, IO]


class Fingerprint:
    """
    Generate unique fingerprints for files to enable resumable uploads.

    Uses SHA-256 hash of file content combined with file size to create
    a unique identifier for each file.
    """

    BLOCK_SIZE = 65536  # 64KB blocks for hashing

    def get_fingerprint(self, file_source: Union[str, IO]) -> str:
        """
        Generate a unique fingerprint for a file.

        Args:
            file_source: Either a file path (str) or file stream (IO)

        Returns:
            str: Unique fingerprint in format "size:{size}--sha256:{hash}"
        """
        if isinstance(file_source, str):
            # file_source is a path
            with open(file_source, "rb") as fs:
                return self._fingerprint_from_stream(fs)
        else:
            # file_source is a stream
            original_pos = file_source.tell()
            try:
                fingerprint = self._fingerprint_from_stream(file_source)
                return fingerprint
            finally:
                file_source.seek(original_pos)

    def _fingerprint_from_stream(self, fs: IO) -> str:
        """Generate fingerprint from file stream."""
        fs.seek(0)
        hasher = hashlib.sha256()

        # Hash full file content in blocks
        while buf := fs.read(self.BLOCK_SIZE):
            if isinstance(buf, str):
                buf = buf.encode("utf-8")
            hasher.update(buf)

        # Get file size
        fs.seek(0, os.SEEK_END)
        file_size = fs.tell()

        return f"size:{file_size}--sha256:{hasher.hexdigest()}"


class PartialMD5Fingerprint(Fingerprint):
    """Cheaper strategy: MD5 of the first N bytes plus the file size.

    Matches tus-py-client's default fingerprint format
    (``size:<n>--md5:<hex>``) so URL-storage entries can interoperate with
    that client if you share the same storage file. Collisions are more
    likely than the full-SHA-256 default, but for the resume-URL lookup
    use case a false match just causes a server-side 404 at worst.
    """

    def __init__(self, probe_bytes: int = 64 * 1024) -> None:
        self.probe_bytes = probe_bytes

    def get_fingerprint(self, file_source: FileSource) -> str:
        if isinstance(file_source, str):
            size = os.path.getsize(file_source)
            with open(file_source, "rb") as f:
                head = f.read(self.probe_bytes)
        else:
            original_pos = file_source.tell()
            try:
                file_source.seek(0, os.SEEK_END)
                size = file_source.tell()
                file_source.seek(0)
                head = file_source.read(self.probe_bytes)
                if isinstance(head, str):
                    head = head.encode("utf-8")
            finally:
                file_source.seek(original_pos)
        digest = hashlib.md5(head).hexdigest()
        return f"size:{size}--md5:{digest}"


class CallableFingerprint(Fingerprint):
    """Adapter that forwards to a user-supplied fingerprint function.

    Lets callers plug in domain-specific identity schemes (e.g. "user
    id + filename", a CDN URL, a database primary key) without having to
    subclass ``Fingerprint``.
    """

    def __init__(self, fn: Callable[[FileSource], str]) -> None:
        self._fn = fn

    def get_fingerprint(self, file_source: FileSource) -> str:
        return self._fn(file_source)
