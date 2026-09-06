"""TUS protocol uploader for fine-grained upload control."""

import os
import ssl
import threading
from threading import Lock
from typing import IO, Callable, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from resumable_upload.client import _protocol
from resumable_upload.client.stats import UploadStats
from resumable_upload.exceptions import TusCommunicationError, TusUploadFailed


class _OffsetMismatch(Exception):
    """Internal: server returned 409 — caller must re-sync offset before retrying."""


class Uploader:
    """TUS protocol uploader for fine-grained upload control.

    This class provides a standalone uploader that can be used independently
    when the upload URL is already known. It allows for manual chunk-by-chunk
    upload control.

    Example:
        >>> # Standalone usage
        >>> uploader = Uploader(
        ...     file_path="file.bin",
        ...     url="http://localhost:8080/files/abc123",
        ...     chunk_size=1024 * 1024,
        ...     headers={"Authorization": "Bearer token"},
        ... )
        >>> uploader.upload_chunk()  # Upload single chunk
        >>> uploader.upload()  # Upload entire file

        >>> # Created from client
        >>> client = TusClient("http://localhost:8080/files")
        >>> uploader = client.create_uploader("file.bin")
        >>> uploader.upload_chunk()
    """

    TUS_VERSION = "1.0.0"

    def __init__(
        self,
        url: str,
        file_path: Optional[str] = None,
        file_stream: Optional[IO] = None,
        chunk_size: Union[int, float] = 1024 * 1024,
        checksum: Union[bool, str] = True,
        metadata_encoding: str = "utf-8",
        headers: Optional[dict[str, str]] = None,
        max_retries: int = 0,
        retry_delay: float = 1.0,
        ssl_context: Optional[ssl.SSLContext] = None,
        timeout: float = 30.0,
        stop_event: Optional[threading.Event] = None,
        before_request: Optional[Callable[[str, str, dict[str, str]], None]] = None,
        after_response: Optional[Callable[[str, str, int], None]] = None,
        on_should_retry: Optional[Callable[[Exception, int], bool]] = None,
        override_patch_method: bool = False,
        add_request_id: bool = False,
    ):
        """Initialize TUS uploader.

        Args:
            url: Upload URL (must already exist on server)
            file_path: Path to file to upload (required if file_stream not provided)
            file_stream: File stream to upload (alternative to file_path)
            chunk_size: Size of upload chunks in bytes (default: 1MB)
            checksum: Enable checksum verification (default: True)
            metadata_encoding: Encoding for metadata values (default: utf-8)
            headers: Optional custom headers to include in all requests
            max_retries: Maximum retry attempts for failed chunks (default: 0, disabled)
            retry_delay: Base delay between retry attempts in seconds (default: 1.0)
            ssl_context: Optional SSL context for TLS connections
            timeout: Request timeout in seconds (default: 30.0)
            stop_event: Optional threading.Event; when set, retry waits are interrupted
            override_patch_method: Send PATCH as POST + ``X-HTTP-Method-Override``
            add_request_id: Unique ``X-Request-ID`` per request (user header wins)

        Raises:
            ValueError: If neither file_path nor file_stream provided, or chunk_size < 1
        """
        if not file_path and not file_stream:
            raise ValueError("Either file_path or file_stream must be provided")

        if file_path and not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        if chunk_size < 1:
            raise ValueError(f"chunk_size must be at least 1 byte, got {chunk_size}")

        self.url = url
        self.file_path = file_path
        self.file_stream = file_stream
        self.chunk_size = int(chunk_size)
        self.checksum = checksum
        self.metadata_encoding = metadata_encoding
        self.headers = headers or {}
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.ssl_context = ssl_context
        self.timeout = timeout
        self._stop_event = stop_event or threading.Event()
        self._before_request = before_request
        self._after_response = after_response
        self._on_should_retry = on_should_retry
        self.override_patch_method = override_patch_method
        self.add_request_id = add_request_id

        # Initialize file stream and get file size
        if file_stream:
            self._file_handle = file_stream
            self._owns_file = False
            file_stream.seek(0, os.SEEK_END)
            self.file_size = file_stream.tell()
            file_stream.seek(0)
        else:
            assert file_path is not None
            self._file_handle = open(file_path, "rb")  # noqa: SIM115
            self._owns_file = True
            self.file_size = os.path.getsize(file_path)

        # Statistics tracking (must be after file_size is set)
        self._stats = UploadStats(total_bytes=self.file_size)
        self.stats_lock = Lock()

        # Set when the server upload was created with Upload-Defer-Length; the
        # committing PATCH must then carry Upload-Length (set in _get_offset).
        self._length_deferred = False

        # Get current offset from server; close file handle on failure
        try:
            self.offset = self._get_offset()
        except Exception:
            if self._owns_file:
                self._file_handle.close()
            raise
        self._file_handle.seek(self.offset)

        # Initialize stats with current offset (for already started uploads)
        self._update_stats_after_chunk()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close file if we own it."""
        self.close()

    def close(self) -> None:
        """Close the file handle if we own it."""
        if self._owns_file and self._file_handle and not self._file_handle.closed:
            self._file_handle.close()

    def _get_offset(self) -> int:
        """Get the current upload offset from server."""
        headers = {
            "Tus-Resumable": self.TUS_VERSION,
            **self.headers,
        }
        _protocol.maybe_add_request_id(headers, self.add_request_id)

        try:
            req = Request(self.url, headers=headers, method="HEAD")
            with urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                offset = response.headers.get("Upload-Offset")
                if offset is None:
                    raise TusCommunicationError("Server did not return Upload-Offset header")
                # A deferred upload advertises Upload-Defer-Length until its
                # length is committed; remember so the next PATCH carries it.
                self._length_deferred = response.headers.get("Upload-Defer-Length") == "1"
                return int(offset)
        except (HTTPError, URLError) as e:
            raise TusCommunicationError(
                f"Failed to get offset: {str(e)}",
            ) from e

    def _upload_chunk(self, data: bytes) -> None:
        """Upload a chunk of data with optional retry logic.

        After successful upload, self.offset will be updated.
        Stats are automatically updated after successful upload.
        """
        if self.max_retries > 0:
            self._upload_chunk_with_retry(data)
        else:
            self._upload_chunk_once(data)
            # Update stats after successful upload
            self._update_stats_after_chunk()

    def _resolve_checksum_algorithm(self) -> Optional[str]:
        """Normalize ``self.checksum`` to a hashlib algorithm name or None."""
        return _protocol.resolve_checksum_algorithm(self.checksum)

    def _upload_chunk_once(self, data: bytes) -> None:
        """Upload a chunk of data (single attempt)."""
        headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Upload-Offset": str(self.offset),
            "Content-Type": "application/offset+octet-stream",
            "Content-Length": str(len(data)),
            **self.headers,
        }

        # Commit the length of a deferred upload on its first PATCH, as the
        # spec requires. Cleared once the server accepts it.
        if self._length_deferred:
            headers["Upload-Length"] = str(self.file_size)

        # Add checksum if enabled. Accepts True (→ sha1) or a hashlib-supported
        # algorithm name (e.g. "sha256", "md5", "sha512").
        algo = self._resolve_checksum_algorithm()
        if algo is not None:
            headers["Upload-Checksum"] = _protocol.checksum_header(algo, data)

        _protocol.maybe_add_request_id(headers, self.add_request_id)

        # X-HTTP-Method-Override: tunnel PATCH through POST for environments
        # whose proxies/firewalls reject PATCH. The server rewrites it back.
        method = "PATCH"
        if self.override_patch_method:
            method = "POST"
            headers["X-HTTP-Method-Override"] = "PATCH"

        if self._before_request is not None:
            self._before_request(method, self.url, headers)
        try:
            req = Request(self.url, data=data, headers=headers, method=method)
            with urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                if self._after_response is not None:
                    self._after_response(method, self.url, response.status)
                # Update offset from server response
                new_offset = response.headers.get("Upload-Offset")
                if new_offset:
                    self.offset = int(new_offset)
                else:
                    self.offset += len(data)
                # Length is now committed server-side; don't resend it.
                self._length_deferred = False
        except HTTPError as e:
            if e.code == 409:
                raise _OffsetMismatch(
                    f"Server offset mismatch at local offset {self.offset}: {e}"
                ) from e
            raise TusUploadFailed(
                f"Failed to upload chunk at offset {self.offset}: {e}",
            ) from e
        except URLError as e:
            raise TusUploadFailed(
                f"Failed to upload chunk at offset {self.offset}: {e}",
            ) from e

    def _update_stats_after_chunk(self) -> None:
        """Update statistics after a chunk is successfully uploaded.

        This method should be called after self.offset has been updated
        to reflect the new upload position.
        """
        with self.stats_lock:
            # uploaded_bytes should always match offset
            self._stats.uploaded_bytes = self.offset

            # Calculate chunks_completed based on current offset
            # Use ceiling division to count partial last chunks correctly
            # E.g. 15 bytes with 10-byte chunks: ceil(15/10) = 2 chunks
            if self.offset > 0:
                self._stats.chunks_completed = -(-self.offset // self.chunk_size)
            else:
                self._stats.chunks_completed = 0

    def _upload_chunk_with_retry(self, data: bytes) -> None:
        """Upload a chunk of data with retry logic."""
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):  # +1 for initial attempt
            try:
                self._upload_chunk_once(data)
                # Track retry if this was a retry attempt
                if attempt > 0:
                    with self.stats_lock:
                        self._stats.chunks_retried += 1
                # Update stats after successful upload
                self._update_stats_after_chunk()
                return  # Success
            except _OffsetMismatch:
                raise  # Don't retry 409; caller must re-sync offset via HEAD
            except (TusUploadFailed, OSError) as e:
                last_error = e
                # User-defined veto: skip the remaining retry budget entirely.
                if self._on_should_retry is not None and not self._on_should_retry(e, attempt + 1):
                    with self.stats_lock:
                        self._stats.chunks_failed += 1
                    raise TusUploadFailed(
                        f"Retry vetoed by on_should_retry at offset {self.offset}: {e}"
                    ) from e
                if attempt < self.max_retries:
                    # Exponential backoff capped at 60 seconds; interruptible via stop_event
                    delay = _protocol.retry_delay(self.retry_delay, attempt)
                    if self._stop_event.wait(timeout=delay):
                        raise TusUploadFailed("Upload cancelled via stop_event") from e
                else:
                    # All retries failed
                    with self.stats_lock:
                        self._stats.chunks_failed += 1
                    raise TusUploadFailed(
                        f"Failed to upload chunk at offset {self.offset} "
                        f"after {self.max_retries + 1} attempts: {str(e)}",
                    ) from e

        # Should not reach here, but just in case
        error_msg = str(last_error) if last_error else "Unknown error"
        raise TusUploadFailed(f"Failed to upload chunk at offset {self.offset}: {error_msg}")

    def upload_chunk(self) -> bool:
        """Upload a single chunk.

        Returns:
            True if more chunks remain, False if upload is complete

        Raises:
            TusUploadFailed: If upload fails
        """
        if self.offset >= self.file_size:
            return False

        # Read chunk
        chunk_size = min(self.chunk_size, self.file_size - self.offset)
        self._file_handle.seek(self.offset)
        chunk = self._file_handle.read(chunk_size)

        if not chunk:
            raise OSError(
                f"Unexpected end of file at offset {self.offset} "
                f"(file size reported as {self.file_size} bytes)"
            )

        try:
            # Upload chunk (stats are automatically updated inside _upload_chunk)
            self._upload_chunk(chunk)
        except _OffsetMismatch:
            # Server offset diverged (409); re-sync via HEAD
            self.offset = self._get_offset()
            self._file_handle.seek(self.offset)
            self._update_stats_after_chunk()

        return self.offset < self.file_size

    def upload(
        self,
        progress_callback: Optional[Callable[[UploadStats], None]] = None,
        stop_at: Optional[int] = None,
    ) -> str:
        """Upload the entire file or remaining chunks.

        Args:
            progress_callback: Optional callback function that receives UploadStats
            stop_at: Stop upload at this byte offset (for partial uploads)

        Returns:
            Upload URL

        Raises:
            TusUploadFailed: If upload fails
        """
        max_offset = min(stop_at, self.file_size) if stop_at is not None else self.file_size

        while self.offset < max_offset:
            chunk_size = min(self.chunk_size, max_offset - self.offset)
            self._file_handle.seek(self.offset)
            chunk = self._file_handle.read(chunk_size)

            if not chunk:
                raise OSError(
                    f"Unexpected end of file at offset {self.offset} "
                    f"(file size reported as {self.file_size} bytes)"
                )

            try:
                # Upload chunk (stats are automatically updated inside _upload_chunk)
                self._upload_chunk(chunk)
            except _OffsetMismatch:
                # Server offset diverged (409); re-sync via HEAD and retry chunk
                self.offset = self._get_offset()
                self._file_handle.seek(self.offset)
                self._update_stats_after_chunk()
                continue

            if progress_callback:
                progress_callback(self.stats)

        # A deferred-length upload of an empty file runs zero loop iterations,
        # so Upload-Length is never committed and the upload stays incomplete
        # forever. Send one final empty PATCH to commit the length.
        if self._length_deferred and self.offset >= self.file_size:
            self._upload_chunk(b"")

        return self.url

    @property
    def stats(self) -> UploadStats:
        """Get upload statistics.

        Returns:
            UploadStats object with current upload statistics (read-only copy)
        """
        with self.stats_lock:
            # Return a copy to prevent external modification
            return UploadStats(
                total_bytes=self._stats.total_bytes,
                uploaded_bytes=self._stats.uploaded_bytes,
                chunks_completed=self._stats.chunks_completed,
                chunks_failed=self._stats.chunks_failed,
                chunks_retried=self._stats.chunks_retried,
                start_time=self._stats.start_time,
            )

    @property
    def is_complete(self) -> bool:
        """Check if upload is complete.

        Returns:
            True if upload is complete, False otherwise
        """
        return self.offset >= self.file_size
