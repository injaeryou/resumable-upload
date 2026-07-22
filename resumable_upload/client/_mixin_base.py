"""Shared attribute declarations for ``TusClient`` mixins.

Each mixin (``ProtocolMixin``, ``ConcatenationMixin``, ``ParallelUploadMixin``)
inherits from :class:`_ClientAttrs` so ``self.X`` references inside the mixin
methods resolve to declared types under ``ty``. The actual values come from
the concrete ``TusClient.__init__``; this module only declares the shape.
"""

import ssl
from typing import IO, Callable, Optional, Union

from resumable_upload.client.stats import UploadStats
from resumable_upload.fingerprint import Fingerprint
from resumable_upload.url_storage import URLStorage


class _ClientAttrs:
    """Type-only stubs of attributes/methods provided by ``TusClient``.

    Mixins inherit from this class so type-checkers see ``self.X`` resolved.
    The bodies raise ``NotImplementedError`` because the concrete subclass
    (``TusClient``) overrides every method; this base class is never used
    standalone.
    """

    TUS_VERSION: str = ""

    url: str
    chunk_size: int
    checksum: Union[bool, str]
    verify_tls_cert: bool
    metadata_encoding: str
    store_url: bool
    url_storage: Optional[URLStorage]
    fingerprinter: Fingerprint
    headers: dict[str, str]
    max_retries: int
    retry_delay: float
    timeout: float
    before_request: Optional[Callable[[str, str, dict[str, str]], None]]
    after_response: Optional[Callable[[str, str, int], None]]
    on_should_retry: Optional[Callable[[Exception, int], bool]]
    override_patch_method: bool
    add_request_id: bool
    on_upload_url_available: Optional[Callable[[str], None]]
    ssl_context: Optional[ssl.SSLContext]

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
        raise NotImplementedError

    def get_file_size(self, file_source: Union[str, IO]) -> int:
        raise NotImplementedError

    def _create_upload(
        self,
        file_size: int,
        metadata: dict[str, str],
        initial_data: Optional[bytes] = None,
        extra_headers: Optional[dict[str, str]] = None,
        defer_length: bool = False,
    ) -> str:
        raise NotImplementedError

    def encode_metadata(self, metadata: dict[str, str]) -> list:
        raise NotImplementedError

    def create_final_upload(
        self,
        partial_urls: list[str],
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        raise NotImplementedError
