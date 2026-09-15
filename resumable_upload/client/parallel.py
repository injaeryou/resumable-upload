"""Parallel chunk-upload helper.

Mixed into :class:`TusClient`. Implements the TUS concatenation extension
client side: upload N partial slices in parallel, merge server-side via a
single final-creation request.
"""

import os
from typing import Callable, Optional

from resumable_upload.client import _protocol
from resumable_upload.client._fileslice import FileSlice
from resumable_upload.client._mixin_base import _ClientAttrs
from resumable_upload.client.stats import UploadStats
from resumable_upload.client.uploader import Uploader
from resumable_upload.exceptions import TusCommunicationError


class ParallelUploadMixin(_ClientAttrs):
    """Implements ``_upload_parallel`` used by ``upload_file``."""

    def _upload_parallel(
        self,
        file_path: str,
        metadata: dict[str, str],
        parallel_uploads: int,
        progress_callback: Optional[Callable[[UploadStats], None]],
        metadata_for_partial_uploads: Optional[dict[str, str]] = None,
    ) -> str:
        """Split a file into N ranges, upload concurrently, and merge server-side."""
        from concurrent.futures import ThreadPoolExecutor

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

        # Cross-session resume (tus-js-client's parallelUploadUrls): the
        # partial URLs are remembered under a side key until the merge, after
        # which the final URL takes the plain fingerprint slot.
        fingerprint = self.fingerprinter.get_fingerprint(file_path) if self.store_url else None
        stored: Optional[list[str]] = None
        if fingerprint is not None:
            assert self.url_storage is not None
            final_url = self.url_storage.get_url(fingerprint)
            if final_url:
                try:
                    self.get_upload_info(final_url)
                    return final_url
                except TusCommunicationError as e:
                    if e.status_code not in (404, 410):
                        raise
                    self.url_storage.remove_url(fingerprint)
            stored = _protocol.decode_partials(
                self.url_storage.get_url(_protocol.partials_key(fingerprint))
            )
            if stored is not None and len(stored) != len(boundaries):
                stored = None  # split changed; the old partials are useless

        def _open(url: str, lo: int, hi: int) -> tuple[Uploader, FileSlice]:
            # Stream the slice from disk on demand instead of reading it all
            # into memory; the uploader does not own the stream, so close it here.
            file_slice = FileSlice(file_path, lo, hi - lo)
            try:
                uploader = Uploader(
                    url=url,
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
                    override_patch_method=self.override_patch_method,
                    add_request_id=self.add_request_id,
                )
            except BaseException:
                file_slice.close()
                raise
            return uploader, file_slice

        def _close(opened: list[tuple[Uploader, FileSlice]]) -> None:
            for uploader, file_slice in opened:
                uploader.close()
                file_slice.close()

        # Open every slice up front, sequentially: a stale partial is recreated
        # here, and the URL list is persisted before any payload byte moves.
        partial_urls: list[str] = []
        opened: list[tuple[Uploader, FileSlice]] = []
        try:
            for i, (lo, hi) in enumerate(boundaries):
                url = stored[i] if stored else None
                pair = None
                if url:
                    try:
                        pair = _open(url, lo, hi)
                    except TusCommunicationError as e:
                        if e.status_code not in (404, 410):
                            raise
                if pair is None:
                    # Partials carry metadata only when explicitly requested
                    # (tus-js-client's metadataForPartialUploads); the real
                    # metadata is attached to the final upload.
                    url = self._create_upload(
                        hi - lo,
                        metadata=metadata_for_partial_uploads or {},
                        extra_headers={"Upload-Concat": "partial"},
                    )
                    pair = _open(url, lo, hi)
                assert url is not None
                partial_urls.append(url)
                opened.append(pair)
            if fingerprint is not None:
                assert self.url_storage is not None
                self.url_storage.set_url(
                    _protocol.partials_key(fingerprint), _protocol.encode_partials(partial_urls)
                )

            def run(uploader: Uploader) -> None:
                uploader.upload()

            with ThreadPoolExecutor(max_workers=parallel_uploads) as pool:
                for future in [pool.submit(run, uploader) for uploader, _ in opened]:
                    future.result()
        finally:
            _close(opened)

        if progress_callback:
            stats = UploadStats(total_bytes=file_size)
            stats.uploaded_bytes = file_size
            progress_callback(stats)

        final_url = self.create_final_upload(partial_urls=partial_urls, metadata=metadata)
        if fingerprint is not None:
            assert self.url_storage is not None
            self.url_storage.set_url(fingerprint, final_url)
            self.url_storage.remove_url(_protocol.partials_key(fingerprint))
        return final_url
