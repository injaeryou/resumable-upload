"""Async TUS protocol uploader for fine-grained upload control."""

from __future__ import annotations

import asyncio
import os
from threading import Lock
from typing import IO, Any, Callable

from resumable_upload.client import _protocol
from resumable_upload.client.aio import _http
from resumable_upload.client.stats import UploadStats
from resumable_upload.exceptions import TusCommunicationError, TusUploadFailed


class _OffsetMismatch(Exception):
    """Internal: server returned 409 — caller must re-sync offset before retrying."""


class AsyncUploader:
    """Async TUS protocol uploader for fine-grained upload control.

    The httpx AsyncClient is INJECTED via the constructor; AsyncUploader does NOT
    own or close it — that is the caller's responsibility.

    Do not instantiate directly. Use the async factory instead::

        up = await AsyncUploader.open(client, url, file_stream=stream)
        await up.upload()
    """

    TUS_VERSION = "1.0.0"

    def __init__(
        self,
        client: Any,
        url: str,
        *,
        file_path: str | None = None,
        file_stream: IO[bytes] | None = None,
        chunk_size: int | float = 1024 * 1024,
        checksum: bool | str = True,
        metadata_encoding: str = "utf-8",
        headers: dict[str, str] | None = None,
        max_retries: int = 0,
        retry_delay: float = 1.0,
        timeout: float = 30.0,
        stop_event: asyncio.Event | None = None,
        before_request: Callable[[str, str, dict[str, str]], None] | None = None,
        after_response: Callable[[str, str, int], None] | None = None,
        on_should_retry: Callable[[Exception, int], bool] | None = None,
    ) -> None:
        if not file_path and not file_stream:
            raise ValueError("Either file_path or file_stream must be provided")

        if chunk_size < 1:
            raise ValueError(f"chunk_size must be at least 1 byte, got {chunk_size}")

        self._client = client
        self.url = url
        self.file_path = file_path
        self.file_stream = file_stream
        self.chunk_size = int(chunk_size)
        self.checksum = checksum
        self.metadata_encoding = metadata_encoding
        self.headers = headers or {}
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self._stop_event = stop_event or asyncio.Event()
        self._before_request = before_request
        self._after_response = after_response
        self._on_should_retry = on_should_retry

        # Populated by _init_io / open()
        self._file_handle: IO[bytes] | None = None
        self._owns_file: bool = False
        self.file_size: int = 0
        self.offset: int = 0
        self._stats: UploadStats | None = None
        self.stats_lock = Lock()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        client: Any,
        url: str,
        **kwargs: Any,
    ) -> AsyncUploader:
        """Async factory: build an AsyncUploader, resolve file size, fetch offset.

        Parameters mirror the constructor. Use this instead of constructing directly.
        """
        self = cls(client, url, **kwargs)
        await self._init_io()
        try:
            self.offset = await self._get_offset()
            fh = self._file_handle
            assert fh is not None
            await asyncio.to_thread(fh.seek, self.offset)
        except BaseException:
            await self.aclose()
            raise
        self._update_stats_after_chunk()
        return self

    # ------------------------------------------------------------------
    # I/O initialisation (called from open())
    # ------------------------------------------------------------------

    async def _init_io(self) -> None:
        """Open the file handle and determine file size. Runs file I/O via to_thread."""
        if self.file_stream is not None:
            # In-memory stream: size can be determined synchronously
            self._file_handle = self.file_stream
            self._owns_file = False
            self.file_stream.seek(0, os.SEEK_END)
            self.file_size = self.file_stream.tell()
            self.file_stream.seek(0)
        else:
            assert self.file_path is not None
            file_path: str = self.file_path

            # File on disk: open + stat via thread so we don't block the loop
            def _open_file() -> tuple[IO[bytes], int]:
                fh = open(file_path, "rb")  # noqa: SIM115
                size = os.path.getsize(file_path)
                return fh, size

            self._file_handle, self.file_size = await asyncio.to_thread(_open_file)
            self._owns_file = True

        self._stats = UploadStats(total_bytes=self.file_size)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_at(self, offset: int, n: int) -> bytes:
        """Synchronous read used inside asyncio.to_thread."""
        assert self._file_handle is not None
        self._file_handle.seek(offset)
        return self._file_handle.read(n)

    async def _get_offset(self) -> int:
        """GET the current upload offset from the server via HEAD."""
        headers = {
            "Tus-Resumable": self.TUS_VERSION,
            **self.headers,
        }
        resp = await _http.request(
            self._client, "HEAD", self.url, headers=headers, timeout=self.timeout
        )
        if resp.status_code >= 400:
            raise TusCommunicationError(f"Failed to get offset: server returned {resp.status_code}")
        offset = resp.headers.get("Upload-Offset")
        if offset is None:
            raise TusCommunicationError("Server did not return Upload-Offset header")
        return int(offset)

    def _update_stats_after_chunk(self) -> None:
        """Update statistics after self.offset has advanced."""
        assert self._stats is not None
        with self.stats_lock:
            self._stats.uploaded_bytes = self.offset
            if self.offset > 0:
                self._stats.chunks_completed = -(-self.offset // self.chunk_size)
            else:
                self._stats.chunks_completed = 0

    async def _upload_chunk_once(self, data: bytes) -> None:
        """Single PATCH attempt. Raises _OffsetMismatch on 409, TusUploadFailed on other errors."""
        headers: dict[str, str] = {
            "Tus-Resumable": self.TUS_VERSION,
            "Upload-Offset": str(self.offset),
            "Content-Type": "application/offset+octet-stream",
            "Content-Length": str(len(data)),
            **self.headers,
        }

        algo = _protocol.resolve_checksum_algorithm(self.checksum)
        if algo is not None:
            headers["Upload-Checksum"] = _protocol.checksum_header(algo, data)

        if self._before_request is not None:
            self._before_request("PATCH", self.url, headers)

        resp = await _http.request(
            self._client, "PATCH", self.url, headers=headers, content=data, timeout=self.timeout
        )

        if resp.status_code == 409:
            raise _OffsetMismatch(
                f"Server offset mismatch at local offset {self.offset}: {resp.status_code}"
            )
        if resp.status_code >= 400:
            raise TusUploadFailed(
                f"Failed to upload chunk at offset {self.offset}: {resp.status_code}"
            )

        if self._after_response is not None:
            self._after_response("PATCH", self.url, resp.status_code)

        new_offset = resp.headers.get("Upload-Offset")
        if new_offset:
            self.offset = int(new_offset)
        else:
            self.offset += len(data)

    async def _upload_chunk(self, data: bytes) -> None:
        """Upload a chunk, optionally with retry. Updates stats on success."""
        if self.max_retries > 0:
            await self._upload_chunk_with_retry(data)
        else:
            await self._upload_chunk_once(data)
            self._update_stats_after_chunk()

    async def _upload_chunk_with_retry(self, data: bytes) -> None:
        """Upload a chunk with exponential backoff retry."""
        assert self._stats is not None
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                await self._upload_chunk_once(data)
                if attempt > 0:
                    with self.stats_lock:
                        self._stats.chunks_retried += 1
                self._update_stats_after_chunk()
                return
            except _OffsetMismatch:
                raise  # Don't retry 409; caller must re-sync offset via HEAD
            except (TusUploadFailed, OSError) as e:
                last_error = e
                if self._on_should_retry is not None and not self._on_should_retry(e, attempt + 1):
                    with self.stats_lock:
                        self._stats.chunks_failed += 1
                    raise TusUploadFailed(
                        f"Retry vetoed by on_should_retry at offset {self.offset}: {e}"
                    ) from e
                if attempt < self.max_retries:
                    delay = _protocol.retry_delay(self.retry_delay, attempt)
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                        # Event was set → cancel requested
                        raise TusUploadFailed("Upload cancelled via stop_event") from e
                    except asyncio.TimeoutError:
                        pass  # Wait elapsed; proceed to next attempt
                else:
                    with self.stats_lock:
                        self._stats.chunks_failed += 1
                    raise TusUploadFailed(
                        f"Failed to upload chunk at offset {self.offset} "
                        f"after {self.max_retries + 1} attempts: {str(e)}",
                    ) from e

        error_msg = str(last_error) if last_error else "Unknown error"
        raise TusUploadFailed(f"Failed to upload chunk at offset {self.offset}: {error_msg}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upload_chunk(self) -> bool:
        """Upload a single chunk.

        Returns:
            True if more chunks remain, False if upload is complete.

        Raises:
            TusUploadFailed: If upload fails.
        """
        if self.offset >= self.file_size:
            return False

        chunk_size = min(self.chunk_size, self.file_size - self.offset)
        chunk = await asyncio.to_thread(self._read_at, self.offset, chunk_size)

        if not chunk:
            raise OSError(
                f"Unexpected end of file at offset {self.offset} "
                f"(file size reported as {self.file_size} bytes)"
            )

        try:
            await self._upload_chunk(chunk)
        except _OffsetMismatch:
            # Server offset diverged (409); re-sync via HEAD
            self.offset = await self._get_offset()
            fh = self._file_handle
            assert fh is not None
            await asyncio.to_thread(fh.seek, self.offset)
            self._update_stats_after_chunk()

        return self.offset < self.file_size

    async def upload(
        self,
        progress_callback: Callable[[UploadStats], None] | None = None,
        stop_at: int | None = None,
    ) -> str:
        """Upload the entire file or remaining chunks.

        Args:
            progress_callback: Optional callback that receives UploadStats after each chunk.
            stop_at: Stop upload at this byte offset (for partial uploads).

        Returns:
            Upload URL.

        Raises:
            TusUploadFailed: If upload fails.
        """
        max_offset = min(stop_at, self.file_size) if stop_at is not None else self.file_size

        while self.offset < max_offset:
            chunk_size = min(self.chunk_size, max_offset - self.offset)
            chunk = await asyncio.to_thread(self._read_at, self.offset, chunk_size)

            if not chunk:
                raise OSError(
                    f"Unexpected end of file at offset {self.offset} "
                    f"(file size reported as {self.file_size} bytes)"
                )

            try:
                await self._upload_chunk(chunk)
            except _OffsetMismatch:
                # Server offset diverged (409); re-sync via HEAD and retry chunk
                self.offset = await self._get_offset()
                fh = self._file_handle
                assert fh is not None
                await asyncio.to_thread(fh.seek, self.offset)
                self._update_stats_after_chunk()
                continue

            if progress_callback:
                progress_callback(self.stats)

        return self.url

    async def aclose(self) -> None:
        """Close the owned file handle (if any) via a thread."""
        fh = self._file_handle
        if self._owns_file and fh is not None and not fh.closed:
            await asyncio.to_thread(fh.close)

    @property
    def stats(self) -> UploadStats:
        """Get upload statistics (read-only snapshot)."""
        assert self._stats is not None
        with self.stats_lock:
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
        """True when upload offset has reached file size."""
        return self.offset >= self.file_size
