"""Parallel chunk-upload helper.

Mixed into :class:`TusClient`. Implements the TUS concatenation extension
client side: upload N partial slices in parallel, merge server-side via a
single final-creation request.
"""

import os
from typing import Callable, Optional

from resumable_upload.client import _protocol
from resumable_upload.client._mixin_base import _ClientAttrs
from resumable_upload.client.stats import UploadStats
from resumable_upload.client.uploader import Uploader


class ParallelUploadMixin(_ClientAttrs):
    """Implements ``_upload_parallel`` used by ``upload_file``."""

    def _upload_parallel(
        self,
        file_path: str,
        metadata: dict[str, str],
        parallel_uploads: int,
        progress_callback: Optional[Callable[[UploadStats], None]],
    ) -> str:
        """Split a file into N ranges, upload concurrently, and merge server-side."""
        from concurrent.futures import ThreadPoolExecutor

        from resumable_upload.client._fileslice import FileSlice

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            # Degenerate case — fall back to a single zero-length upload.
            return self.upload_file(
                file_path,
                metadata=metadata,
                parallel_uploads=1,
                progress_callback=progress_callback,
            )

        # Compute byte boundaries; the last slice absorbs any remainder.
        # Slices start at `start` and end at `end` (exclusive).
        boundaries = _protocol.split_boundaries(file_size, parallel_uploads)

        if "filename" not in metadata:
            metadata = {**metadata, "filename": os.path.basename(file_path)}

        def upload_slice(lo: int, hi: int) -> str:
            length = hi - lo
            # Send partials without metadata; metadata is attached to the final.
            upload_url = self._create_upload(
                length, metadata={}, extra_headers={"Upload-Concat": "partial"}
            )
            # Stream the slice from disk on demand instead of reading it all
            # into memory; the uploader does not own the stream, so close it here.
            file_slice = FileSlice(file_path, lo, length)
            uploader = Uploader(
                url=upload_url,
                file_stream=file_slice,  # ty: ignore[invalid-argument-type]
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
                uploader.upload()
                return upload_url
            finally:
                uploader.close()
                file_slice.close()

        with ThreadPoolExecutor(max_workers=parallel_uploads) as pool:
            futures = [pool.submit(upload_slice, lo, hi) for lo, hi in boundaries]
            partial_urls = [f.result() for f in futures]

        if progress_callback:
            stats = UploadStats(total_bytes=file_size)
            stats.uploaded_bytes = file_size
            progress_callback(stats)

        return self.create_final_upload(partial_urls=partial_urls, metadata=metadata)
