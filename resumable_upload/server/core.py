"""TUS protocol server implementation."""

import asyncio
import logging
import threading
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Any, BinaryIO, Callable, Optional

from resumable_upload.checksum import ChecksumAlgorithms
from resumable_upload.exceptions import TusHookError
from resumable_upload.locks import LockBackend
from resumable_upload.metrics import MetricsRegistry
from resumable_upload.server.handlers import (
    handle_create,
    handle_create_async,
    handle_delete,
    handle_delete_async,
    handle_head,
    handle_head_async,
    handle_options,
    handle_options_async,
    handle_patch,
    handle_patch_async,
)
from resumable_upload.server.handlers.create import (
    handle_create_final,
    handle_create_final_async,
)
from resumable_upload.server.handlers.get import (
    handle_get_download,
    handle_get_download_async,
)
from resumable_upload.server.headers import (
    add_cors_headers,
    format_expiry,
    parse_metadata,
    validate_upload_id,
)
from resumable_upload.storage import SQLiteStorage, Storage

logger = logging.getLogger(__name__)


class TusServerCore:
    """TUS protocol server core.

    Implements the full TUS 1.0.0 protocol surface: creation, termination,
    checksum, expiration, creation-with-upload, creation-defer-length, and
    concatenation. Subclass either this class or :class:`TusServer` to
    customize behavior; the two are interchangeable today, with ``TusServer``
    reserved as the natural extension point for future features.

    Version Handling:
        - Supports only TUS version 1.0.0
        - Requires clients to send "Tus-Resumable: 1.0.0" header
        - Returns 412 Precondition Failed for other versions

    Example:
        >>> storage = SQLiteStorage()
        >>> server = TusServer(storage=storage, base_path="/files")
        >>> status, headers, body = server.handle_request(
        ...     "POST",
        ...     "/files",
        ...     {"tus-resumable": "1.0.0", "upload-length": "1024"},
        ...     b"",
        ... )
    """

    TUS_VERSION = "1.0.0"
    _MAX_METADATA_SIZE = 4096  # 4 KB limit to guard against DoS
    SUPPORTED_EXTENSIONS = [
        "creation",
        "creation-with-upload",
        "creation-defer-length",
        "termination",
        "checksum",
        "expiration",
        "concatenation",
    ]

    def __init__(
        self,
        storage: Optional[Storage] = None,
        base_path: str = "/files",
        max_size: int = 0,
        max_chunk_size: int = 0,
        upload_expiry: Optional[int] = None,
        cors_allow_origins: Optional[str] = None,
        cleanup_interval: int = 60,
        request_timeout: int = 30,
        on_incoming_request: Optional[Callable[..., None]] = None,
        on_upload_create: Optional[Callable[..., Optional[dict]]] = None,
        on_upload_complete: Optional[Callable[..., Optional[dict]]] = None,
        on_upload_terminate: Optional[Callable[..., None]] = None,
        on_chunk_received: Optional[Callable[..., None]] = None,
        on_before_terminate: Optional[Callable[..., None]] = None,
        metrics_registry: Optional[MetricsRegistry] = None,
        metrics_path: str = "/metrics",
        lock_backend: Optional[LockBackend] = None,
        lock_ttl_seconds: float = 60.0,
        lock_wait_seconds: float = 5.0,
        checksum_algorithms: tuple[str, ...] = ("sha1",),
        supports_checksum_trailer: bool = False,
        enable_downloads: bool = False,
    ):
        """Initialize TUS server.

        Args:
            storage: Storage backend (defaults to SQLiteStorage)
            base_path: Base URL path for uploads
            max_size: Maximum upload size in bytes (0 = unlimited)
            max_chunk_size: Maximum individual chunk size in bytes (0 = unlimited)
            upload_expiry: Upload expiry in seconds (None = no expiry)
            cors_allow_origins: CORS allowed origins (None = no CORS headers)
            cleanup_interval: Minimum seconds between expired-upload cleanup runs (default: 60)
            request_timeout: Socket read timeout in seconds for HTTP handler (default: 30)
            on_incoming_request: Called before processing any request.
                Signature: (method: str, path: str, headers: dict) -> None.
                Raise TusHookError to reject the request.
            on_upload_create: Called before creating an upload.
                Signature: (upload_id: str, metadata: dict, upload_length: int) -> Optional[dict].
                Return a dict to replace metadata, None to keep original.
                Raise TusHookError to reject creation.
            on_upload_complete: Called after an upload is fully completed.
                Signature: (upload_id: str, metadata: dict, file_info: dict)
                -> Optional[dict]. Return a dict with any of ``status_code``,
                ``headers``, ``body`` to customize the finishing request's
                response (tusd's pre-finish). Exceptions are logged but do
                not affect the client response.
            on_upload_terminate: Called after an upload is deleted.
                Signature: (upload_id: str) -> None.
                Exceptions are logged but do not affect the client response.
            on_chunk_received: Called after every accepted PATCH chunk
                (tusd's post-receive). Signature:
                (upload_id: str, offset: int, chunk_size: int) -> None.
                Raise TusHookError to stop the upload: the upload is deleted
                and the error status returned (tusd's StopUpload). Other
                exceptions are logged and ignored.
            on_before_terminate: Called before a DELETE is honored
                (tusd's pre-terminate). Signature: (upload_id: str) -> None.
                Raise TusHookError to veto the termination.
        """
        self.storage = storage or SQLiteStorage()
        self.base_path = base_path.rstrip("/")
        # Conditionally advertised extensions:
        # - concatenation-unfinished needs a storage backend that can create
        #   pending finals and assemble them later.
        # - checksum-trailer needs a transport that parses HTTP trailers
        #   (the bundled TusHTTPRequestHandler does; set the flag when yours
        #   does too and merges the trailing Upload-Checksum into headers).
        self.supports_checksum_trailer = supports_checksum_trailer
        # Non-standard download endpoint (tusd-style GET). Opt-in because the
        # library is usually embedded next to framework GET routes.
        self.enable_downloads = enable_downloads
        extensions = list(type(self).SUPPORTED_EXTENSIONS)
        if getattr(self.storage, "supports_unfinished_concat", False):
            extensions.append("concatenation-unfinished")
        if supports_checksum_trailer:
            extensions.append("checksum-trailer")
        self.SUPPORTED_EXTENSIONS = extensions
        self.max_size = max_size
        self.max_chunk_size = max_chunk_size
        self.upload_expiry = upload_expiry
        self.cors_allow_origins = cors_allow_origins
        self.cleanup_interval = cleanup_interval
        self.request_timeout = request_timeout
        self._last_cleanup: Optional[datetime] = None
        self._cleanup_lock = threading.Lock()
        # Async path uses a non-blocking flag instead of the lock above: holding
        # a threading.Lock across an await would block the event loop (deadlock
        # when cleanup_interval <= 0). Single event loop => a plain flag suffices.
        self._cleanup_running = False
        self._on_incoming_request = on_incoming_request
        self._on_upload_create = on_upload_create
        self._on_upload_complete = on_upload_complete
        self._on_upload_terminate = on_upload_terminate
        self._on_chunk_received = on_chunk_received
        self._on_before_terminate = on_before_terminate
        self._metrics = metrics_registry
        self._metrics_path = metrics_path
        self._locks = lock_backend
        self._lock_ttl = lock_ttl_seconds
        self._lock_wait = lock_wait_seconds
        self._checksums = ChecksumAlgorithms(checksum_algorithms)
        if self._metrics is not None:
            self._metrics.register_counter("tusd_requests_total", "Total HTTP requests received")
            self._metrics.register_counter(
                "tusd_errors_total", "Total responses with status >= 400"
            )
            self._metrics.register_counter(
                "tusd_uploads_created_total", "Uploads created (POST succeeded)"
            )
            self._metrics.register_counter(
                "tusd_uploads_finished_total",
                "Uploads completed (final chunk received or final upload merged)",
            )
            self._metrics.register_counter(
                "tusd_uploads_terminated_total", "Uploads terminated via DELETE"
            )
            self._metrics.register_counter(
                "tusd_bytes_received_total", "Total bytes received across all PATCH requests"
            )

    @property
    def metrics(self) -> Optional[MetricsRegistry]:
        return self._metrics

    @property
    def metrics_path(self) -> str:
        return self._metrics_path

    def _invoke_pre_hook(self, hook: Callable, *args: Any) -> Any:
        """Invoke a pre-hook, converting exceptions to HTTP error responses.

        Returns the hook's return value on success, or raises to signal
        that the caller should return an error response.
        """
        try:
            return hook(*args)
        except TusHookError:
            raise
        except Exception:
            hook_name = getattr(hook, "__name__", repr(hook))
            logger.exception("Unexpected error in pre-hook %s", hook_name)
            raise TusHookError("Internal Server Error", status_code=500) from None

    def _invoke_post_hook(self, hook: Callable, *args: Any) -> Any:
        """Invoke a post-hook, catching and logging all exceptions.

        Returns the hook's return value (None when the hook raised) so
        completion hooks can customize the finishing response.
        """
        try:
            return hook(*args)
        except Exception:
            logger.exception("Error in post-hook %s", getattr(hook, "__name__", repr(hook)))
            return None

    @staticmethod
    def _apply_completion_response(
        hook_result: Any, response: tuple[int, dict[str, str], bytes]
    ) -> tuple[int, dict[str, str], bytes]:
        """Merge an on_upload_complete dict result into the finishing response."""
        if not isinstance(hook_result, dict):
            return response
        status, headers, body = response
        status = hook_result.get("status_code", status)
        extra_headers = hook_result.get("headers")
        if isinstance(extra_headers, dict):
            headers = {**headers, **extra_headers}
        raw_body = hook_result.get("body")
        if raw_body is not None:
            body = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
        if body and status == 204:
            # RFC 9110 forbids content on 204; a hook attaching a body
            # without an explicit status_code would otherwise corrupt
            # keep-alive connections on strict HTTP stacks.
            status = 200
        if body:
            headers = {**headers, "Content-Length": str(len(body))}
        return (status, headers, body)

    def terminate_upload(self, upload_id: str) -> bool:
        """Server-initiated termination (out-of-band StopUpload).

        Deletes the upload and fires on_upload_terminate. Returns True when
        the upload existed. Bypasses on_before_terminate — that hook guards
        *client* DELETEs; the server operator calling this has already decided.
        """
        if not self.storage.get_upload(upload_id):
            return False
        self.storage.delete_upload(upload_id)
        if self._metrics is not None:
            self._metrics.inc("tusd_uploads_terminated_total")
        if self._on_upload_terminate:
            self._invoke_post_hook(self._on_upload_terminate, upload_id)
        logger.info("Server-initiated termination of upload %s", upload_id)
        return True

    def _validate_upload_id(self, upload_id: str) -> bool:
        """Validate that upload_id is a valid UUID to prevent path traversal."""
        return validate_upload_id(upload_id)

    def _error_response(self, status: int, message: str) -> tuple[int, dict, bytes]:
        """Build a consistent error response with Tus-Resumable header."""
        return (status, {"Tus-Resumable": self.TUS_VERSION}, message.encode())

    def _with_lock(
        self, upload_id: str, fn: Callable[[], tuple[int, dict, bytes]]
    ) -> tuple[int, dict, bytes]:
        """Wrap a write-path handler in a distributed lock when configured.

        Returns 423 Locked if another holder is already writing and the wait
        timeout elapses. A no-op wrapper when lock_backend is unset.
        """
        if self._locks is None:
            return fn()
        token = self._locks.acquire(
            upload_id, ttl_seconds=self._lock_ttl, wait_timeout=self._lock_wait
        )
        if token is None:
            return self._error_response(423, "Upload locked; retry later")
        try:
            return fn()
        finally:
            self._locks.release(upload_id, token)

    async def _with_lock_async(
        self,
        upload_id: str,
        fn: Callable[[], Awaitable[tuple[int, dict, bytes]]],
    ) -> tuple[int, dict, bytes]:
        """Async sibling of :meth:`_with_lock`. ``LockBackend`` stays sync;
        ``acquire``/``release`` run on a worker thread while the wrapped
        coroutine is awaited under the held lock.
        """
        if self._locks is None:
            return await fn()
        token = await asyncio.to_thread(
            self._locks.acquire,
            upload_id,
            ttl_seconds=self._lock_ttl,
            wait_timeout=self._lock_wait,
        )
        if token is None:
            return self._error_response(423, "Upload locked; retry later")
        try:
            return await fn()
        finally:
            await asyncio.to_thread(self._locks.release, upload_id, token)

    def _add_cors_headers(self, headers: dict) -> dict:
        """Add CORS headers if cors_allow_origins is configured."""
        return add_cors_headers(headers, self.cors_allow_origins)

    def _format_expiry(self, expires_at: datetime) -> str:
        """Format expiry datetime as RFC 7231 date string."""
        return format_expiry(expires_at)

    def _parse_metadata(self, upload_metadata: str) -> tuple[Optional[dict[str, str]], str]:
        """Parse the Upload-Metadata header value."""
        return parse_metadata(upload_metadata, self._MAX_METADATA_SIZE)

    def handle_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes = b""
    ) -> tuple[int, dict[str, str], "bytes | BinaryIO"]:
        """Handle an incoming HTTP request.

        Args:
            method: HTTP method
            path: Request path
            headers: Request headers
            body: Request body

        Returns:
            Tuple of (status_code, response_headers, response_body)
        """
        logger.info("Received %s request for %s", method, path)

        # Normalize headers to lowercase
        headers = {k.lower(): v for k, v in headers.items()}

        # Record the request under its wire method (before any override rewrite).
        if self._metrics is not None:
            self._metrics.inc("tusd_requests_total", labels={"method": method})

        # X-HTTP-Method-Override: let clients tunnel PATCH/DELETE/HEAD through
        # POST for environments (CDNs, WAFs, legacy proxies) that block those
        # methods. Only POST may be rewritten, and only to PATCH/DELETE/HEAD —
        # rewriting to OPTIONS/GET would sidestep the Tus-Resumable check.
        if method == "POST":
            override = headers.get("x-http-method-override", "").strip().upper()
            if override:
                allowed_overrides = {"PATCH", "DELETE", "HEAD"}
                if override not in allowed_overrides:
                    status, resp_headers, resp_body = self._error_response(
                        400, f"Unsupported X-HTTP-Method-Override value: {override}"
                    )
                    return (status, self._add_cors_headers(resp_headers), resp_body)
                method = override

        # Early body-size gate for direct API callers (frameworks that pre-read the body)
        if method == "PATCH" and self.max_chunk_size > 0 and len(body) > self.max_chunk_size:
            return self._error_response(413, "Chunk exceeds maximum chunk size")
        if self.max_size > 0 and len(body) > self.max_size:
            return self._error_response(413, "Request entity too large")

        # Invoke on_incoming_request hook before any processing
        if self._on_incoming_request:
            try:
                self._invoke_pre_hook(self._on_incoming_request, method, path, headers)
            except TusHookError as e:
                return (
                    e.status_code,
                    self._add_cors_headers({"Tus-Resumable": self.TUS_VERSION}),
                    e.body.encode(),
                )

        # Check TUS version (required by TUS spec for all non-OPTIONS requests).
        # GET is exempt too: the download endpoint serves plain HTTP clients
        # (browsers) that never send Tus-Resumable.
        if method not in ("OPTIONS", "GET"):
            tus_version = headers.get("tus-resumable")
            if tus_version != self.TUS_VERSION:
                logger.warning(
                    "Invalid TUS version: %s, expected %s", tus_version, self.TUS_VERSION
                )
                status, resp_headers, resp_body = (
                    412,
                    {"Tus-Resumable": self.TUS_VERSION},
                    b"Precondition Failed: Invalid TUS version",
                )
                return (status, self._add_cors_headers(resp_headers), resp_body)

        # Route request
        if method == "OPTIONS":
            result = self._handle_options(path, headers)
        elif method == "POST" and path in (self.base_path, self.base_path + "/"):
            # Tolerate a trailing slash on the creation endpoint — tusd does,
            # and reference clients (tus-py-client) build the URL that way.
            result = self._handle_create(headers, body)
        elif method == "HEAD" and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = self._handle_head(upload_id, headers)
        elif method == "PATCH" and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = self._with_lock(
                    upload_id, lambda: self._handle_patch(upload_id, headers, body)
                )
        elif method == "GET" and self.enable_downloads and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = self._handle_get_download(upload_id, headers)
        elif method == "DELETE" and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = self._with_lock(upload_id, lambda: self._handle_delete(upload_id, headers))
        else:
            logger.warning("Route not found: %s %s", method, path)
            result = self._error_response(404, "Not Found")

        status, resp_headers, resp_body = result

        # Periodically clean up expired uploads after the current request is handled,
        # so the current request still gets 410 for an expired upload before it's deleted
        if self.upload_expiry is not None:
            now = datetime.now(timezone.utc)
            if (
                self._last_cleanup is None
                or (now - self._last_cleanup).total_seconds() >= self.cleanup_interval
            ):
                with self._cleanup_lock:
                    # Re-check after acquiring lock (double-check pattern)
                    if (
                        self._last_cleanup is None
                        or (now - self._last_cleanup).total_seconds() >= self.cleanup_interval
                    ):
                        self._last_cleanup = now
                        count = self.storage.cleanup_expired_uploads()
                        if count:
                            logger.info("Cleaned up %s expired upload(s)", count)

        if self._metrics is not None and status >= 400:
            self._metrics.inc("tusd_errors_total", labels={"status": str(status)})

        return (status, self._add_cors_headers(resp_headers), resp_body)

    async def handle_request_async(
        self, method: str, path: str, headers: dict[str, str], body: bytes = b""
    ) -> tuple[int, dict[str, str], "bytes | BinaryIO"]:
        """Async sibling of :meth:`handle_request`.

        Mirrors the same protocol surface but awaits storage I/O so true-async
        backends (overrides on Storage's ``*_async`` methods) gain non-blocking
        behavior end-to-end. Default sync backends fall back to the
        ``asyncio.to_thread`` wrappers on the Storage ABC — same throughput as
        :meth:`handle_request`.
        """
        logger.info("Received %s request for %s", method, path)

        headers = {k.lower(): v for k, v in headers.items()}

        if self._metrics is not None:
            self._metrics.inc("tusd_requests_total", labels={"method": method})

        if method == "POST":
            override = headers.get("x-http-method-override", "").strip().upper()
            if override:
                allowed_overrides = {"PATCH", "DELETE", "HEAD"}
                if override not in allowed_overrides:
                    status, resp_headers, resp_body = self._error_response(
                        400, f"Unsupported X-HTTP-Method-Override value: {override}"
                    )
                    return (status, self._add_cors_headers(resp_headers), resp_body)
                method = override

        if method == "PATCH" and self.max_chunk_size > 0 and len(body) > self.max_chunk_size:
            return self._error_response(413, "Chunk exceeds maximum chunk size")
        if self.max_size > 0 and len(body) > self.max_size:
            return self._error_response(413, "Request entity too large")

        if self._on_incoming_request:
            try:
                self._invoke_pre_hook(self._on_incoming_request, method, path, headers)
            except TusHookError as e:
                return (
                    e.status_code,
                    self._add_cors_headers({"Tus-Resumable": self.TUS_VERSION}),
                    e.body.encode(),
                )

        if method not in ("OPTIONS", "GET"):
            tus_version = headers.get("tus-resumable")
            if tus_version != self.TUS_VERSION:
                logger.warning(
                    "Invalid TUS version: %s, expected %s", tus_version, self.TUS_VERSION
                )
                status, resp_headers, resp_body = (
                    412,
                    {"Tus-Resumable": self.TUS_VERSION},
                    b"Precondition Failed: Invalid TUS version",
                )
                return (status, self._add_cors_headers(resp_headers), resp_body)

        if method == "OPTIONS":
            result = await self._handle_options_async(path, headers)
        elif method == "POST" and path in (self.base_path, self.base_path + "/"):
            # Trailing slash tolerated — matches tusd and tus-py-client.
            result = await self._handle_create_async(headers, body)
        elif method == "HEAD" and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = await self._handle_head_async(upload_id, headers)
        elif method == "PATCH" and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = await self._with_lock_async(
                    upload_id,
                    lambda: self._handle_patch_async(upload_id, headers, body),
                )
        elif method == "GET" and self.enable_downloads and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = await self._handle_get_download_async(upload_id, headers)
        elif method == "DELETE" and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = await self._with_lock_async(
                    upload_id,
                    lambda: self._handle_delete_async(upload_id, headers),
                )
        else:
            logger.warning("Route not found: %s %s", method, path)
            result = self._error_response(404, "Not Found")

        status, resp_headers, resp_body = result

        if self.upload_expiry is not None:
            now = datetime.now(timezone.utc)
            if not self._cleanup_running and (
                self._last_cleanup is None
                or (now - self._last_cleanup).total_seconds() >= self.cleanup_interval
            ):
                self._cleanup_running = True
                self._last_cleanup = now
                try:
                    count = await self.storage.cleanup_expired_uploads_async()
                    if count:
                        logger.info("Cleaned up %s expired upload(s)", count)
                finally:
                    self._cleanup_running = False

        if self._metrics is not None and status >= 400:
            self._metrics.inc("tusd_errors_total", labels={"status": str(status)})

        return (status, self._add_cors_headers(resp_headers), resp_body)

    # --- Per-method delegates ---------------------------------------------
    # These exist so subclasses can override a single method without rewiring
    # the dispatch in ``handle_request``. The real bodies live in
    # ``resumable_upload.server.handlers``.

    def _handle_options(
        self, path: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return handle_options(self, path, headers)

    def _handle_create(
        self, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        return handle_create(self, headers, body)

    def _handle_create_final(
        self, concat_header: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return handle_create_final(self, concat_header, headers)

    def _handle_head(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return handle_head(self, upload_id, headers)

    def _handle_patch(
        self, upload_id: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        return handle_patch(self, upload_id, headers, body)

    def _handle_delete(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return handle_delete(self, upload_id, headers)

    def _handle_get_download(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], "bytes | BinaryIO"]:
        return handle_get_download(self, upload_id, headers)

    # --- Async per-method delegates ---------------------------------------

    async def _handle_options_async(
        self, path: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return await handle_options_async(self, path, headers)

    async def _handle_create_async(
        self, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        return await handle_create_async(self, headers, body)

    async def _handle_create_final_async(
        self, concat_header: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return await handle_create_final_async(self, concat_header, headers)

    async def _handle_head_async(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return await handle_head_async(self, upload_id, headers)

    async def _handle_patch_async(
        self, upload_id: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        return await handle_patch_async(self, upload_id, headers, body)

    async def _handle_delete_async(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        return await handle_delete_async(self, upload_id, headers)

    async def _handle_get_download_async(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], "bytes | BinaryIO"]:
        return await handle_get_download_async(self, upload_id, headers)
