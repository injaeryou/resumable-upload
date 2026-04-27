"""Client-side TUS concatenation extension helpers.

Mixed into :class:`TusClient`. Holds the partial-upload + final-creation
helpers that implement the concatenation extension on the client side.
"""

from typing import IO, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from resumable_upload.client._mixin_base import _ClientAttrs
from resumable_upload.client.stats import UploadStats
from resumable_upload.client.uploader import Uploader
from resumable_upload.exceptions import TusCommunicationError


class ConcatenationMixin(_ClientAttrs):
    """Implements partial / final upload helpers for concatenation."""

    def create_partial_upload(
        self,
        file_path: Optional[str] = None,
        file_stream: Optional[IO] = None,
        metadata: Optional[dict[str, str]] = None,
        progress_callback: Optional[Callable[[UploadStats], None]] = None,
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
        """
        if not file_path and not file_stream:
            raise ValueError("Either file_path or file_stream must be provided")

        file_size = self.get_file_size(file_path or file_stream)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

        upload_url = self._create_upload(
            file_size,
            metadata or {},
            extra_headers={"Upload-Concat": "partial"},
        )

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
        )
        try:
            uploader.upload(progress_callback=progress_callback)
            return uploader.url
        finally:
            uploader.close()

    def create_final_upload(
        self,
        partial_urls: list[str],
        metadata: Optional[dict[str, str]] = None,
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
        encoded_metadata = self.encode_metadata(metadata or {})

        headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Upload-Concat": concat_header,
            **self.headers,
        }
        if encoded_metadata:
            headers["Upload-Metadata"] = ",".join(encoded_metadata)

        try:
            req = Request(self.url, headers=headers, method="POST")
            with urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                location: Optional[str] = response.headers.get("Location")
                if not location:
                    raise TusCommunicationError("Server did not return Location header")
                if not location.startswith("http"):
                    location = urljoin(self.url, location)
                return location
        except (HTTPError, URLError) as e:
            raise TusCommunicationError(f"Failed to create final upload: {e}") from e
