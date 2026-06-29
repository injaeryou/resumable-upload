"""Async TUS protocol client implementation (requires httpx)."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import Callable
from typing import IO, Any
from urllib.parse import urljoin

from resumable_upload.client import _protocol
from resumable_upload.client.aio import _http
from resumable_upload.client.aio.uploader import AsyncUploader
from resumable_upload.client.stats import UploadStats
from resumable_upload.exceptions import TusCommunicationError
from resumable_upload.fingerprint import Fingerprint
from resumable_upload.url_storage import FileURLStorage, URLStorage


def _read_slice(path: str, lo: int, length: int) -> bytes:
    """Read ``length`` bytes from ``path`` starting at byte ``lo``.

    Designed to be called via ``asyncio.to_thread`` so the blocking
    file I/O is offloaded from the event loop.
    """
    with open(path, "rb") as f:
        f.seek(lo)
        return f.read(length)


class AsyncTusClient:
    """Async TUS protocol client for uploading files.

    This client implements TUS protocol version 1.0.0 as specified at:
    https://tus.io/protocols/resumable-upload.html

    Uses httpx as the HTTP transport; httpx is imported lazily so the
    package core remains dependency-free (requires the ``[async]`` extra).

    Use as an async context manager to ensure the underlying httpx
    ``AsyncClient`` is closed properly::

        async with AsyncTusClient("http://localhost:8080/files") as client:
            url = await client.upload_file("large_file.bin")

    Args:
        url: Base URL of the TUS server.
        chunk_size: Upload chunk size in bytes (default: 1 MB).
        checksum: Enable or select a checksum algorithm (default: True → sha1).
        verify_tls_cert: Verify TLS certificates (default: True).
        metadata_encoding: Encoding for metadata values (default: utf-8).
        store_url: Persist upload URLs for resumability (default: False).
        url_storage: Custom URLStorage implementation.
        fingerprinter: Custom Fingerprint implementation.
        headers: Extra headers sent with every request.
        max_retries: Maximum retry attempts per chunk (default: 3).
        retry_delay: Base delay (seconds) between retries (default: 1.0).
        timeout: Request timeout in seconds (default: 30.0).
        before_request: Hook called before every request.
        after_response: Hook called after every response.
        on_should_retry: Hook consulted before each retry.
        _transport: *Private test-only* — inject a custom ``httpx`` transport
            (e.g. ``httpx.ASGITransport``) so tests run in-process without a
            real socket. Do not use in production code.
    """

    TUS_VERSION = "1.0.0"

    def __init__(
        self,
        url: str,
        chunk_size: int | float = 1024 * 1024,
        checksum: bool | str = True,
        verify_tls_cert: bool = True,
        metadata_encoding: str = "utf-8",
        store_url: bool = False,
        url_storage: URLStorage | None = None,
        fingerprinter: Fingerprint | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        timeout: float = 30.0,
        before_request: Callable[[str, str, dict[str, str]], None] | None = None,
        after_response: Callable[[str, str, int], None] | None = None,
        on_should_retry: Callable[[Exception, int], bool] | None = None,
        # Private test-only injection seam; do not use in production.
        _transport: Any | None = None,
    ) -> None:
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be at least 1 byte, got {chunk_size}")
        self.url = url.rstrip("/")
        self.chunk_size = int(chunk_size)
        self.checksum = checksum
        self.verify_tls_cert = verify_tls_cert
        self.metadata_encoding = metadata_encoding
        self.store_url = store_url
        if store_url and url_storage is None:
            url_storage = FileURLStorage()
        self.url_storage = url_storage
        self.fingerprinter = fingerprinter or Fingerprint()
        self.headers = headers or {}
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.before_request = before_request
        self.after_response = after_response
        self.on_should_retry = on_should_retry
        self._transport = _transport
        self._client: Any | None = None  # httpx.AsyncClient, built lazily

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AsyncTusClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying httpx AsyncClient."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> Any:
        """Build the httpx AsyncClient on first use (lazy import)."""
        if self._client is not None:
            return self._client

        httpx = _http.import_httpx()
        if self._transport is not None:
            # Test-only: use an injected transport (e.g. ASGITransport)
            self._client = httpx.AsyncClient(
                transport=self._transport,
                base_url=self.url,
            )
        else:
            self._client = httpx.AsyncClient(verify=self.verify_tls_cert)
        return self._client

    async def _create_upload(
        self,
        file_size: int,
        metadata: dict[str, str],
        initial_data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        defer_length: bool = False,
    ) -> str:
        """POST to the TUS endpoint to create a new upload.

        Returns the resolved absolute upload URL from the Location header.
        """
        encoded_metadata = _protocol.encode_metadata(metadata, self.metadata_encoding)

        headers: dict[str, str] = {
            "Tus-Resumable": self.TUS_VERSION,
            **self.headers,
        }
        if defer_length:
            headers["Upload-Defer-Length"] = "1"
        else:
            headers["Upload-Length"] = str(file_size)
        if extra_headers:
            headers.update(extra_headers)
        if encoded_metadata:
            headers["Upload-Metadata"] = ",".join(encoded_metadata)

        content = b""
        if initial_data is not None:
            content = initial_data
            headers["Content-Type"] = "application/offset+octet-stream"
            headers["Content-Length"] = str(len(initial_data))

        if self.before_request is not None:
            self.before_request("POST", self.url, headers)

        client = await self._ensure_client()
        resp = await _http.request(
            client, "POST", self.url, headers=headers, content=content, timeout=self.timeout
        )

        if self.after_response is not None:
            self.after_response("POST", self.url, resp.status_code)

        if resp.status_code >= 400:
            raise TusCommunicationError(
                f"Failed to create upload: server returned {resp.status_code}"
            )

        location: str | None = resp.headers.get("Location")
        if not location:
            raise TusCommunicationError("Server did not return Location header")

        if not location.startswith("http"):
            location = urljoin(self.url, location)

        return location

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        file_path: str | None = None,
        file_stream: IO[bytes] | None = None,
        metadata: dict[str, str] | None = None,
        progress_callback: Callable[[UploadStats], None] | None = None,
        stop_at: int | None = None,
        parallel_uploads: int = 1,
    ) -> str:
        """Upload a file to the TUS server.

        Args:
            file_path: Path to the file to upload (required if file_stream not given).
            file_stream: File-like object to upload (alternative to file_path).
            metadata: Optional metadata dictionary.
            progress_callback: Callback receiving UploadStats after each chunk.
            stop_at: Stop uploading at this byte offset (for partial uploads).
            parallel_uploads: Number of concurrent partial uploads to run.
                When > 1 the file is split into ``parallel_uploads`` byte ranges,
                each uploaded as a TUS partial, then merged server-side via the
                concatenation extension. Requires ``file_path`` (streams are not
                split) and is incompatible with ``stop_at``. Server must support
                the concatenation extension.

        Returns:
            URL of the completed upload.

        Raises:
            ValueError: If neither file_path nor file_stream provided,
                parallel_uploads < 1, parallel_uploads > 1 with a stream,
                or parallel_uploads > 1 with stop_at.
            FileNotFoundError: If file_path does not exist.
            TusCommunicationError: If the upload fails.
        """
        if parallel_uploads < 1:
            raise ValueError(f"parallel_uploads must be >= 1, got {parallel_uploads}")

        if not file_path and not file_stream:
            raise ValueError("Either file_path or file_stream must be provided")

        if file_path and not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        if parallel_uploads > 1:
            if file_path is None:
                raise ValueError("parallel_uploads requires file_path (streams are not split)")
            if stop_at is not None:
                raise ValueError("parallel_uploads is incompatible with stop_at")
            return await self._upload_parallel(
                file_path, metadata or {}, parallel_uploads, progress_callback
            )

        # Determine file size
        if file_stream:
            file_stream.seek(0, os.SEEK_END)
            file_size = file_stream.tell()
            file_stream.seek(0)
        else:
            assert file_path is not None
            file_size = os.path.getsize(file_path)

        metadata = metadata or {}

        if "filename" not in metadata and file_path:
            metadata["filename"] = os.path.basename(file_path)

        fingerprint = (
            await asyncio.to_thread(
                self.fingerprinter.get_fingerprint,
                file_path or file_stream,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )
            if self.store_url
            else None
        )

        upload_url: str | None = None
        if self.store_url:
            assert self.url_storage is not None
            assert fingerprint is not None
            upload_url = self.url_storage.get_url(fingerprint)

        if not upload_url:
            upload_url = await self._create_upload(file_size, metadata)
            if self.store_url:
                assert self.url_storage is not None
                assert fingerprint is not None
                self.url_storage.set_url(fingerprint, upload_url)

        client = await self._ensure_client()
        up = await AsyncUploader.open(
            client,
            upload_url,
            file_path=file_path,
            file_stream=file_stream,
            chunk_size=self.chunk_size,
            checksum=self.checksum,
            metadata_encoding=self.metadata_encoding,
            headers=self.headers.copy(),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            timeout=self.timeout,
            before_request=self.before_request,
            after_response=self.after_response,
            on_should_retry=self.on_should_retry,
        )
        try:
            await up.upload(progress_callback=progress_callback, stop_at=stop_at)
        finally:
            await up.aclose()

        return upload_url

    async def _upload_parallel(
        self,
        file_path: str,
        metadata: dict[str, str],
        parallel_uploads: int,
        progress_callback: Callable[[UploadStats], None] | None,
    ) -> str:
        """Upload a file in parallel slices using asyncio.gather + a Semaphore.

        Splits the file into ``parallel_uploads`` byte ranges, uploads each as
        a TUS partial (concatenation extension), then creates a final upload
        that merges them server-side. Falls back to a single upload for empty
        files.
        """
        file_size = self.get_file_size(file_path)
        if file_size == 0:
            return await self.upload_file(
                file_path,
                metadata=metadata,
                parallel_uploads=1,
                progress_callback=progress_callback,
            )

        boundaries = _protocol.split_boundaries(file_size, parallel_uploads)

        if "filename" not in metadata:
            metadata = {**metadata, "filename": os.path.basename(file_path)}

        sem = asyncio.Semaphore(parallel_uploads)

        async def upload_slice(lo: int, hi: int) -> str:
            async with sem:
                length = hi - lo
                url = await self._create_upload(
                    length, {}, extra_headers={"Upload-Concat": "partial"}
                )
                buf = await asyncio.to_thread(_read_slice, file_path, lo, length)
                client = await self._ensure_client()
                up = await AsyncUploader.open(
                    client,
                    url,
                    file_stream=io.BytesIO(buf),
                    chunk_size=self.chunk_size,
                    checksum=self.checksum,
                    metadata_encoding=self.metadata_encoding,
                    headers=self.headers.copy(),
                    max_retries=self.max_retries,
                    retry_delay=self.retry_delay,
                    timeout=self.timeout,
                    before_request=self.before_request,
                    after_response=self.after_response,
                    on_should_retry=self.on_should_retry,
                )
                try:
                    await up.upload()
                    return url
                finally:
                    await up.aclose()

        partial_urls = await asyncio.gather(*(upload_slice(lo, hi) for lo, hi in boundaries))

        if progress_callback:
            stats = UploadStats(total_bytes=file_size)
            stats.uploaded_bytes = file_size
            progress_callback(stats)

        return await self.create_final_upload(list(partial_urls), metadata=metadata)

    async def resume_upload(
        self,
        file_path: str | None = None,
        upload_url: str = "",
        file_stream: IO[bytes] | None = None,
        progress_callback: Callable[[UploadStats], None] | None = None,
    ) -> str:
        """Resume an interrupted upload.

        Args:
            file_path: Path to the file (required if file_stream not given).
            upload_url: URL of the existing upload to resume.
            file_stream: File-like object (alternative to file_path).
            progress_callback: Callback receiving UploadStats after each chunk.

        Returns:
            URL of the completed upload.

        Raises:
            FileNotFoundError: If file_path does not exist.
            ValueError: If neither file_path nor file_stream is given.
            TusCommunicationError: If the upload fails.
        """
        if file_path and not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        if not file_path and not file_stream:
            raise ValueError("Either file_path or file_stream must be provided")

        client = await self._ensure_client()
        up = await AsyncUploader.open(
            client,
            upload_url,
            file_path=file_path,
            file_stream=file_stream,
            chunk_size=self.chunk_size,
            checksum=self.checksum,
            metadata_encoding=self.metadata_encoding,
            headers=self.headers.copy(),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            timeout=self.timeout,
            before_request=self.before_request,
            after_response=self.after_response,
            on_should_retry=self.on_should_retry,
        )
        try:
            await up.upload(progress_callback=progress_callback)
        finally:
            await up.aclose()

        return upload_url

    async def delete_upload(self, upload_url: str) -> None:
        """Delete an upload from the server. A 404 response is silently tolerated.

        Args:
            upload_url: URL of the upload to delete.

        Raises:
            TusCommunicationError: If deletion fails with a non-404 error status.
        """
        headers: dict[str, str] = {
            "Tus-Resumable": self.TUS_VERSION,
            "Content-Length": "0",
            **self.headers,
        }

        client = await self._ensure_client()
        resp = await _http.request(
            client, "DELETE", upload_url, headers=headers, timeout=self.timeout
        )

        if resp.status_code == 404:
            return  # Already deleted — tolerated per spec
        if resp.status_code >= 400:
            raise TusCommunicationError(
                f"Failed to delete upload: server returned {resp.status_code}"
            )

    async def create_deferred_upload(
        self,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Create an upload without declaring its length up front.

        The length is committed when the first PATCH includes ``Upload-Length``.
        Returns the upload URL.
        """
        return await self._create_upload(file_size=0, metadata=metadata or {}, defer_length=True)

    async def create_partial_upload(
        self,
        file_path: str | None = None,
        file_stream: IO[bytes] | None = None,
        metadata: dict[str, str] | None = None,
        progress_callback: Callable[[UploadStats], None] | None = None,
    ) -> str:
        """Create and fully upload a partial upload (TUS concatenation extension).

        Partial uploads are the building blocks of a concatenated final upload.
        Pair this with :meth:`create_final_upload` to merge them server-side.

        Args:
            file_path: Path to file to upload (required if file_stream not provided).
            file_stream: File stream to upload (alternative to file_path).
            metadata: Optional metadata dictionary sent on creation.
            progress_callback: Optional callback that receives UploadStats.

        Returns:
            URL of the completed partial upload.

        Raises:
            ValueError: If neither file_path nor file_stream is provided.
            TusCommunicationError: If the upload fails.
        """
        if not file_path and not file_stream:
            raise ValueError("Either file_path or file_stream must be provided")

        file_size = self.get_file_size(file_path or file_stream)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

        url = await self._create_upload(
            file_size,
            metadata or {},
            extra_headers={"Upload-Concat": "partial"},
        )

        client = await self._ensure_client()
        up = await AsyncUploader.open(
            client,
            url,
            file_path=file_path,
            file_stream=file_stream,
            chunk_size=self.chunk_size,
            checksum=self.checksum,
            metadata_encoding=self.metadata_encoding,
            headers=self.headers.copy(),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            timeout=self.timeout,
            before_request=self.before_request,
            after_response=self.after_response,
            on_should_retry=self.on_should_retry,
        )
        try:
            await up.upload(progress_callback=progress_callback)
            return up.url
        finally:
            await up.aclose()

    async def create_final_upload(
        self,
        partial_urls: list[str],
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Create a final upload that concatenates the given partial upload URLs.

        The server merges the listed partials (in order) into a single completed
        upload. All partials must already be fully uploaded; incomplete partials
        cause the server to return 400.

        Args:
            partial_urls: Ordered list of partial upload URLs.
            metadata: Metadata to attach to the final upload.

        Returns:
            URL of the new final upload.

        Raises:
            ValueError: If partial_urls is empty.
            TusCommunicationError: If the server rejects the final-creation.
        """
        if not partial_urls:
            raise ValueError("partial_urls must contain at least one URL")

        concat_header = "final;" + " ".join(partial_urls)
        encoded = _protocol.encode_metadata(metadata or {}, self.metadata_encoding)

        headers: dict[str, str] = {
            "Tus-Resumable": self.TUS_VERSION,
            "Upload-Concat": concat_header,
            **self.headers,
        }
        if encoded:
            headers["Upload-Metadata"] = ",".join(encoded)

        client = await self._ensure_client()
        resp = await _http.request(client, "POST", self.url, headers=headers, timeout=self.timeout)

        if resp.status_code >= 400:
            raise TusCommunicationError(
                f"Failed to create final upload: server returned {resp.status_code}"
            )

        location: str | None = resp.headers.get("Location")
        if not location:
            raise TusCommunicationError("Server did not return Location header")

        if not location.startswith("http"):
            location = urljoin(self.url, location)

        return location

    async def create_uploader(
        self,
        file_path: str | None = None,
        file_stream: IO[bytes] | None = None,
        upload_url: str | None = None,
        metadata: dict[str, str] | None = None,
        chunk_size: int | float | None = None,
    ) -> AsyncUploader:
        """Create an AsyncUploader for fine-grained upload control.

        If ``upload_url`` is not provided, a new upload is created on the server.

        Args:
            file_path: Path to the file (required if file_stream not given).
            file_stream: File-like object (alternative to file_path).
            upload_url: Existing upload URL (skips creation when provided).
            metadata: Metadata dictionary (only used when creating a new upload).
            chunk_size: Chunk size override (uses client default when None).

        Returns:
            An AsyncUploader ready to call ``upload()`` on.

        Raises:
            FileNotFoundError: If file_path does not exist.
            TusCommunicationError: If upload creation fails.
        """
        if file_stream:
            file_stream.seek(0, os.SEEK_END)
            file_size = file_stream.tell()
            file_stream.seek(0)
        else:
            if not file_path or not os.path.exists(file_path):
                raise FileNotFoundError(f"File not found: {file_path}")
            file_size = os.path.getsize(file_path)

        if not upload_url:
            metadata = metadata or {}
            if "filename" not in metadata and file_path:
                metadata["filename"] = os.path.basename(file_path)
            upload_url = await self._create_upload(file_size, metadata)

        actual_chunk_size = chunk_size if chunk_size is not None else self.chunk_size
        client = await self._ensure_client()
        return await AsyncUploader.open(
            client,
            upload_url,
            file_path=file_path,
            file_stream=file_stream,
            chunk_size=actual_chunk_size,
            checksum=self.checksum,
            metadata_encoding=self.metadata_encoding,
            headers=self.headers.copy(),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            timeout=self.timeout,
            before_request=self.before_request,
            after_response=self.after_response,
            on_should_retry=self.on_should_retry,
        )

    async def get_metadata(self, upload_url: str) -> dict[str, str]:
        """Get metadata for an upload via HEAD request.

        Args:
            upload_url: URL of the upload.

        Returns:
            Dictionary of metadata key-value pairs.

        Raises:
            TusCommunicationError: If the HEAD request fails.
        """
        headers: dict[str, str] = {
            "Tus-Resumable": self.TUS_VERSION,
            **self.headers,
        }
        client = await self._ensure_client()
        resp = await _http.request(
            client, "HEAD", upload_url, headers=headers, timeout=self.timeout
        )
        if resp.status_code >= 400:
            raise TusCommunicationError(
                f"Failed to get metadata: server returned {resp.status_code}"
            )
        return _protocol.parse_upload_metadata(
            resp.headers.get("Upload-Metadata"), self.metadata_encoding
        )

    async def get_server_info(self) -> dict[str, Any]:
        """Get server information and capabilities via OPTIONS request.

        Returns:
            Dictionary containing:
                - version (str): TUS protocol version supported by server
                - extensions (list[str]): List of supported TUS extensions
                - max_size (int | None): Maximum upload size in bytes (None if unlimited)

        Raises:
            TusCommunicationError: If the OPTIONS request fails.
        """
        client = await self._ensure_client()
        resp = await _http.request(client, "OPTIONS", self.url, headers={}, timeout=self.timeout)
        if resp.status_code >= 400:
            raise TusCommunicationError(
                f"Failed to get server info: server returned {resp.status_code}"
            )
        return _protocol.parse_server_info(
            resp.headers.get("Tus-Version"),
            resp.headers.get("Tus-Extension"),
            resp.headers.get("Tus-Max-Size"),
            self.TUS_VERSION,
        )

    async def get_upload_info(self, upload_url: str) -> dict[str, Any]:
        """Get upload status: offset, length, complete flag, and metadata.

        Args:
            upload_url: URL of the upload.

        Returns:
            Dict with keys ``offset``, ``length``, ``complete``, ``metadata``.

        Raises:
            TusCommunicationError: If the HEAD request fails.
        """
        headers: dict[str, str] = {
            "Tus-Resumable": self.TUS_VERSION,
            **self.headers,
        }
        client = await self._ensure_client()
        resp = await _http.request(
            client, "HEAD", upload_url, headers=headers, timeout=self.timeout
        )
        if resp.status_code >= 400:
            raise TusCommunicationError(
                f"Failed to get upload info: server returned {resp.status_code}"
            )
        return _protocol.parse_upload_info(
            resp.headers.get("Upload-Offset"),
            resp.headers.get("Upload-Length"),
            resp.headers.get("Upload-Metadata"),
            self.metadata_encoding,
        )

    def get_file_size(self, file_source: str | IO[bytes]) -> int:
        """Return the size of a file (synchronous — pure local I/O).

        Args:
            file_source: File path string or a file-like object.

        Returns:
            Size in bytes.
        """
        if isinstance(file_source, str):
            return os.path.getsize(file_source)
        current_pos = file_source.tell()
        file_source.seek(0, os.SEEK_END)
        size = file_source.tell()
        file_source.seek(current_pos)
        return size

    def find_previous_uploads(
        self,
        file_path: str | None = None,
        file_stream: IO[bytes] | None = None,
    ) -> list[dict[str, Any]]:
        """Look up resumable uploads for the given file by fingerprint.

        This method is synchronous because URL storage is local and fast (no I/O
        over the network). Returns a list of ``{fingerprint, upload_url}`` dicts,
        or an empty list when ``store_url`` is off or no match exists.
        """
        if self.url_storage is None:
            return []
        fp = self.fingerprinter.get_fingerprint(
            file_path or file_stream  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )
        url = self.url_storage.get_url(fp)
        if not url:
            return []
        return [{"fingerprint": fp, "upload_url": url}]
