"""TUS protocol client implementation."""

import os
import ssl
from typing import IO, Any, Callable, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from resumable_upload.client import _protocol
from resumable_upload.client.concatenation import ConcatenationMixin
from resumable_upload.client.parallel import ParallelUploadMixin
from resumable_upload.client.protocol import ProtocolMixin
from resumable_upload.client.stats import UploadStats
from resumable_upload.client.uploader import Uploader
from resumable_upload.exceptions import TusCommunicationError
from resumable_upload.fingerprint import Fingerprint
from resumable_upload.url_storage import FileURLStorage, URLStorage


class TusClient(ProtocolMixin, ConcatenationMixin, ParallelUploadMixin):
    """TUS protocol client for uploading files.

    This client implements TUS protocol version 1.0.0 as specified at:
    https://tus.io/protocols/resumable-upload.html

    Version Handling:
        - Uses TUS version 1.0.0
        - Sends "Tus-Resumable: 1.0.0" header with all requests
        - Compatible with TUS 1.0.0 compliant servers
        - Server must support version 1.0.0 to accept uploads

    Features:
        - File upload with configurable chunk size
        - Automatic resume of interrupted uploads
        - Progress tracking via callbacks
        - Optional SHA1 checksum verification
        - Metadata support for file information

    Example:
        >>> client = TusClient("http://localhost:8080/files")
        >>> url = client.upload_file(
        ...     "large_file.bin",
        ...     metadata={"filename": "large_file.bin"},
        ...     progress_callback=lambda up, total: print(f"{up}/{total}"),
        ... )
        >>> # Resume interrupted upload
        >>> client.resume_upload("large_file.bin", url)
    """

    TUS_VERSION = "1.0.0"

    def __init__(
        self,
        url: str,
        chunk_size: Union[int, float] = 1024 * 1024,
        checksum: Union[bool, str] = True,
        verify_tls_cert: bool = True,
        metadata_encoding: str = "utf-8",
        store_url: bool = False,
        url_storage: Optional[URLStorage] = None,
        fingerprinter: Optional[Fingerprint] = None,
        headers: Optional[dict[str, str]] = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        timeout: float = 30.0,
        before_request: Optional[Callable[[str, str, dict[str, str]], None]] = None,
        after_response: Optional[Callable[[str, str, int], None]] = None,
        on_should_retry: Optional[Callable[[Exception, int], bool]] = None,
        override_patch_method: bool = False,
        add_request_id: bool = False,
        on_upload_url_available: Optional[Callable[[str], None]] = None,
    ):
        """Initialize TUS client.

        Args:
            url: Base URL of TUS server
            chunk_size: Size of upload chunks in bytes (default: 1MB). Can be int or float.
            checksum: Enable checksum verification (default: True)
            verify_tls_cert: Verify TLS certificates (default: True)
            metadata_encoding: Encoding for metadata values (default: utf-8)
            store_url: Store upload URLs for resumability (default: False)
            url_storage: Custom URL storage implementation
            fingerprinter: Custom fingerprint implementation
            headers: Optional custom headers to include in all requests
            max_retries: Maximum retry attempts for failed chunks (default: 3)
            retry_delay: Base delay between retry attempts in seconds (default: 1.0)
            timeout: Request timeout in seconds (default: 30.0)
            override_patch_method: Send PATCH as POST + ``X-HTTP-Method-Override``
                for environments whose proxies block PATCH (default: False)
            add_request_id: Add a unique ``X-Request-ID`` UUID to every request
                for log correlation; a user-supplied header wins (default: False)
            on_upload_url_available: Called with the upload URL as soon as it is
                known — right after creation, or when resolved from URL storage

        Raises:
            ValueError: If chunk_size is less than 1
        """
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be at least 1 byte, got {chunk_size}")
        self.url = url.rstrip("/")
        self.chunk_size = int(chunk_size)
        self.checksum = checksum
        self.verify_tls_cert = verify_tls_cert
        self.metadata_encoding = metadata_encoding
        self.store_url = store_url
        # Auto-create FileURLStorage when store_url=True and no storage provided
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
        self.override_patch_method = override_patch_method
        self.add_request_id = add_request_id
        self.on_upload_url_available = on_upload_url_available
        self.ssl_context = self._build_ssl_context()

    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Build SSL context based on verify_tls_cert setting."""
        if not self.verify_tls_cert:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    def upload_file(
        self,
        file_path: Optional[str] = None,
        file_stream: Optional[IO] = None,
        metadata: Optional[dict[str, str]] = None,
        progress_callback: Optional[Callable[[UploadStats], None]] = None,
        stop_at: Optional[int] = None,
        parallel_uploads: int = 1,
        metadata_for_partial_uploads: Optional[dict[str, str]] = None,
    ) -> str:
        """Upload a file to the server.

        Args:
            file_path: Path to file to upload (required if file_stream not provided)
            file_stream: File stream to upload (alternative to file_path)
            metadata: Optional metadata dictionary
            progress_callback: Optional callback function that receives UploadStats
            stop_at: Stop upload at this byte offset (for partial uploads)
            parallel_uploads: Number of concurrent partial uploads to run.
                When > 1 the file is split into ``parallel_uploads`` byte ranges,
                each uploaded as a TUS partial, then merged server-side via the
                concatenation extension. Requires ``file_path`` (streams are
                not split) and is incompatible with ``stop_at``. Server must
                support the concatenation extension.

        Returns:
            URL of the uploaded file

        Raises:
            ValueError: If neither file_path nor file_stream provided,
                parallel_uploads < 1, parallel_uploads > 1 with a stream,
                or parallel_uploads > 1 with stop_at.
            FileNotFoundError: If file doesn't exist
            TusCommunicationError: If upload fails
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
            final_url = self._upload_parallel(
                file_path,
                metadata or {},
                parallel_uploads,
                progress_callback,
                metadata_for_partial_uploads=metadata_for_partial_uploads,
            )
            # The final (merged) upload URL is only known once concatenation
            # happens — fire the callback here, not before the early return.
            if self.on_upload_url_available is not None:
                self.on_upload_url_available(final_url)
            return final_url

        # Get file size
        if file_stream:
            file_stream.seek(0, os.SEEK_END)
            file_size = file_stream.tell()
            file_stream.seek(0)
        else:
            assert file_path is not None
            file_size = os.path.getsize(file_path)

        metadata = metadata or {}

        # Add filename to metadata if not present and we have a file_path
        if "filename" not in metadata and file_path:
            metadata["filename"] = os.path.basename(file_path)

        # Calculate fingerprint once (avoid double computation)
        fingerprint = (
            self.fingerprinter.get_fingerprint(file_path or file_stream)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            if self.store_url
            else None
        )

        # Check for stored URL if enabled
        upload_url = None
        if self.store_url:
            assert self.url_storage is not None
            assert fingerprint is not None
            upload_url = self.url_storage.get_url(fingerprint)

        # Create upload if no stored URL
        if not upload_url:
            upload_url = self._create_upload(file_size, metadata)
            if self.store_url:
                assert self.url_storage is not None
                assert fingerprint is not None
                self.url_storage.set_url(fingerprint, upload_url)

        if self.on_upload_url_available is not None:
            self.on_upload_url_available(upload_url)

        uploader = Uploader(
            url=upload_url,
            file_path=file_path,
            file_stream=file_stream,
            chunk_size=self.chunk_size,
            checksum=self.checksum,
            metadata_encoding=self.metadata_encoding,
            headers=self.headers.copy(),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            ssl_context=self.ssl_context,
            timeout=self.timeout,
            before_request=self.before_request,
            after_response=self.after_response,
            on_should_retry=self.on_should_retry,
            override_patch_method=self.override_patch_method,
            add_request_id=self.add_request_id,
        )

        try:
            uploader.upload(progress_callback=progress_callback, stop_at=stop_at)
            return uploader.url
        finally:
            uploader.close()

    def resume_upload(
        self,
        file_path: Optional[str] = None,
        upload_url: str = "",
        file_stream: Optional[IO] = None,
        progress_callback: Optional[Callable[[UploadStats], None]] = None,
    ) -> str:
        """Resume an interrupted upload.

        Args:
            file_path: Path to file to upload (required if file_stream not provided)
            upload_url: URL of the existing upload
            file_stream: File stream to upload (alternative to file_path)
            progress_callback: Optional callback function that receives UploadStats

        Returns:
            URL of the uploaded file

        Raises:
            FileNotFoundError: If file doesn't exist
            HTTPError: If upload fails
        """
        if file_path and not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        if not file_path and not file_stream:
            raise ValueError("Either file_path or file_stream must be provided")

        uploader = Uploader(
            url=upload_url,
            file_path=file_path,
            file_stream=file_stream,
            chunk_size=self.chunk_size,
            checksum=self.checksum,
            metadata_encoding=self.metadata_encoding,
            headers=self.headers.copy(),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            ssl_context=self.ssl_context,
            timeout=self.timeout,
            before_request=self.before_request,
            after_response=self.after_response,
            on_should_retry=self.on_should_retry,
            override_patch_method=self.override_patch_method,
            add_request_id=self.add_request_id,
        )

        try:
            uploader.upload(progress_callback=progress_callback)
            return uploader.url
        finally:
            uploader.close()

    def delete_upload(self, upload_url: str) -> None:
        """Delete an upload from the server.

        Args:
            upload_url: URL of the upload to delete

        Raises:
            HTTPError: If deletion fails
        """
        headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Content-Length": "0",
            **self.headers,
        }
        _protocol.maybe_add_request_id(headers, self.add_request_id)

        req = Request(upload_url, headers=headers, method="DELETE")
        try:
            with urlopen(req, context=self.ssl_context, timeout=self.timeout):
                pass
        except (HTTPError, URLError) as e:
            if isinstance(e, HTTPError) and e.code == 404:
                return  # Upload already deleted
            raise TusCommunicationError(
                f"Failed to delete upload: {str(e)}",
            ) from e

    def _create_upload(
        self,
        file_size: int,
        metadata: dict[str, str],
        initial_data: Optional[bytes] = None,
        extra_headers: Optional[dict[str, str]] = None,
        defer_length: bool = False,
    ) -> str:
        """Create a new upload on the server.

        When ``defer_length=True`` the request omits ``Upload-Length`` and
        sends ``Upload-Defer-Length: 1``; the caller is then responsible for
        including ``Upload-Length`` on the first PATCH.
        """
        # Encode metadata
        encoded_metadata = self.encode_metadata(metadata)

        headers = {
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

        body = b""
        if initial_data is not None:
            body = initial_data
            headers["Content-Type"] = "application/offset+octet-stream"
            headers["Content-Length"] = str(len(initial_data))

        _protocol.maybe_add_request_id(headers, self.add_request_id)
        if self.before_request is not None:
            self.before_request("POST", self.url, headers)

        try:
            req = Request(self.url, data=body or None, headers=headers, method="POST")
            with urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                if self.after_response is not None:
                    self.after_response("POST", self.url, response.status)
                location: Optional[str] = response.headers.get("Location")
                if not location:
                    raise TusCommunicationError("Server did not return Location header")

                # Handle relative URLs
                if not location.startswith("http"):
                    location = urljoin(self.url, location)

                return location
        except (HTTPError, URLError) as e:
            raise TusCommunicationError(
                f"Failed to create upload: {str(e)}",
            ) from e

    def get_file_size(self, file_source: Union[str, IO]) -> int:
        """
        Get the size of a file.

        Args:
            file_source: Either a file path (str) or file stream (IO)

        Returns:
            File size in bytes
        """
        if isinstance(file_source, str):
            return os.path.getsize(file_source)
        else:
            current_pos = file_source.tell()
            file_source.seek(0, os.SEEK_END)
            size = file_source.tell()
            file_source.seek(current_pos)
            return size

    def get_file_stream(self, file_source: Union[str, IO]) -> IO:
        """
        Get a file stream from a file path or stream.

        Args:
            file_source: Either a file path (str) or file stream (IO)

        Returns:
            File stream object

        Note:
            If file_source is a file path, this method opens the file and returns
            a file handle. The caller is responsible for closing the file handle
            when done. Consider using a context manager:
            >>> with client.get_file_stream("file.txt") as f:
            ...     # use f
        """
        if isinstance(file_source, str):
            return open(file_source, "rb")
        else:
            file_source.seek(0)
            return file_source

    def update_headers(self, headers: dict[str, str]) -> None:
        """Update custom headers for all requests.

        Args:
            headers: Dictionary of header names to values

        Example:
            >>> client = TusClient("http://localhost:8080/files")
            >>> client.update_headers({"Authorization": "Bearer token"})
            >>> # Update existing headers
            >>> client.update_headers({"X-API-Key": "new-key"})
        """
        self.headers.update(headers)

    def get_headers(self) -> dict[str, str]:
        """Get current custom headers.

        Returns:
            Dictionary of current custom headers

        Example:
            >>> client = TusClient("http://localhost:8080/files")
            >>> client.update_headers({"Authorization": "Bearer token"})
            >>> headers = client.get_headers()
            >>> print(headers)  # {"Authorization": "Bearer token"}
        """
        return self.headers.copy()

    def create_uploader(
        self,
        file_path: Optional[str] = None,
        file_stream: Optional[IO] = None,
        upload_url: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
        chunk_size: Optional[Union[int, float]] = None,
    ) -> Uploader:
        """Create an Uploader instance for fine-grained upload control.

        Args:
            file_path: Path to file to upload (required if file_stream not provided)
            file_stream: File stream to upload (alternative to file_path)
            upload_url: Existing upload URL (if None, creates new upload)
            metadata: Optional metadata dictionary (only used if creating new upload)
            chunk_size: Optional chunk size override

        Returns:
            Uploader instance

        Raises:
            ValueError: If neither file_path nor file_stream provided
            FileNotFoundError: If file doesn't exist
            TusCommunicationError: If upload creation fails

        Example:
            >>> client = TusClient("http://localhost:8080/files")
            >>> uploader = client.create_uploader("file.bin")
            >>> uploader.upload_chunk()  # Upload single chunk
            >>> uploader.upload()  # Upload remaining chunks
        """

        # Get file size
        if file_stream:
            file_stream.seek(0, os.SEEK_END)
            file_size = file_stream.tell()
            file_stream.seek(0)
        else:
            if not file_path or not os.path.exists(file_path):
                raise FileNotFoundError(f"File not found: {file_path}")
            file_size = os.path.getsize(file_path)

        # Create upload if URL not provided
        if not upload_url:
            metadata = metadata or {}
            if "filename" not in metadata and file_path:
                metadata["filename"] = os.path.basename(file_path)
            upload_url = self._create_upload(file_size, metadata)
            if self.on_upload_url_available is not None:
                self.on_upload_url_available(upload_url)

        # Create uploader
        actual_chunk_size = chunk_size if chunk_size is not None else self.chunk_size
        return Uploader(
            url=upload_url,
            file_path=file_path,
            file_stream=file_stream,
            chunk_size=actual_chunk_size,
            checksum=self.checksum,
            metadata_encoding=self.metadata_encoding,
            headers=self.headers.copy(),
            max_retries=self.max_retries,
            retry_delay=self.retry_delay,
            ssl_context=self.ssl_context,
            timeout=self.timeout,
            before_request=self.before_request,
            after_response=self.after_response,
            on_should_retry=self.on_should_retry,
            override_patch_method=self.override_patch_method,
            add_request_id=self.add_request_id,
        )

    def find_previous_uploads(
        self,
        file_path: Optional[str] = None,
        file_stream: Optional[IO] = None,
    ) -> list[dict[str, Any]]:
        """Look up resumable uploads for the given file by fingerprint.

        Returns a list of ``{fingerprint, upload_url}`` dicts. Empty when
        ``store_url`` is off, when no URL storage is attached, or when no
        entry matches the file's fingerprint. Analogous to tus-js-client's
        ``findPreviousUploads``.
        """
        if self.url_storage is None:
            return []
        fp = self.fingerprinter.get_fingerprint(file_path or file_stream)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        url = self.url_storage.get_url(fp)
        if not url:
            return []
        return [{"fingerprint": fp, "upload_url": url}]

    def create_deferred_upload(
        self,
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Create an upload without declaring its length up front.

        The length is committed when the client sends the first PATCH with
        an ``Upload-Length`` header. Useful when streaming data whose total
        size is unknown at creation time. Returns the upload URL.
        """
        return self._create_upload(file_size=0, metadata=metadata or {}, defer_length=True)
