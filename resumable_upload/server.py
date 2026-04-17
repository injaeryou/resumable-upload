"""TUS protocol server implementation."""

import base64
import binascii
import hashlib
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Optional

from resumable_upload.exceptions import TusHookError
from resumable_upload.metrics import MetricsRegistry
from resumable_upload.storage import SQLiteStorage, Storage

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_TUS_EXPOSE_HEADERS = (
    "Upload-Offset,Location,Upload-Length,Tus-Version,Tus-Resumable,"
    "Tus-Max-Size,Tus-Extension,Upload-Metadata,Upload-Expires,Upload-Concat"
)
_TUS_ALLOW_HEADERS = (
    "Origin,X-Requested-With,Content-Type,Upload-Length,Upload-Offset,"
    "Tus-Resumable,Upload-Metadata,Upload-Checksum,Upload-Expires,Upload-Concat"
)


class TusServer:
    """TUS protocol server implementation.

    This server implements TUS protocol version 1.0.0 as specified at:
    https://tus.io/protocols/resumable-upload.html

    Version Handling:
        - Supports only TUS version 1.0.0
        - Requires clients to send "Tus-Resumable: 1.0.0" header
        - Returns 412 Precondition Failed for other versions
        - This is compliant with TUS specification

    Supported Extensions:
        - creation: Upload creation via POST
        - termination: Upload deletion via DELETE
        - checksum: SHA1 checksum verification
        - expiration: Upload expiration support
        - creation-with-upload: Initial data in POST body

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
        on_upload_complete: Optional[Callable[..., None]] = None,
        on_upload_terminate: Optional[Callable[..., None]] = None,
        metrics_registry: Optional[MetricsRegistry] = None,
        metrics_path: str = "/metrics",
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
                Signature: (upload_id: str, metadata: dict, file_info: dict) -> None.
                Exceptions are logged but do not affect the client response.
            on_upload_terminate: Called after an upload is deleted.
                Signature: (upload_id: str) -> None.
                Exceptions are logged but do not affect the client response.
        """
        self.storage = storage or SQLiteStorage()
        self.base_path = base_path.rstrip("/")
        self.max_size = max_size
        self.max_chunk_size = max_chunk_size
        self.upload_expiry = upload_expiry
        self.cors_allow_origins = cors_allow_origins
        self.cleanup_interval = cleanup_interval
        self.request_timeout = request_timeout
        self._last_cleanup: Optional[datetime] = None
        self._cleanup_lock = threading.Lock()
        self._on_incoming_request = on_incoming_request
        self._on_upload_create = on_upload_create
        self._on_upload_complete = on_upload_complete
        self._on_upload_terminate = on_upload_terminate
        self._metrics = metrics_registry
        self._metrics_path = metrics_path
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

    def _invoke_post_hook(self, hook: Callable, *args: Any) -> None:
        """Invoke a post-hook, catching and logging all exceptions."""
        try:
            hook(*args)
        except Exception:
            logger.exception("Error in post-hook %s", getattr(hook, "__name__", repr(hook)))

    def _validate_upload_id(self, upload_id: str) -> bool:
        """Validate that upload_id is a valid UUID to prevent path traversal."""
        return bool(_UUID_RE.match(upload_id))

    def _error_response(self, status: int, message: str) -> tuple[int, dict, bytes]:
        """Build a consistent error response with Tus-Resumable header."""
        return (status, {"Tus-Resumable": self.TUS_VERSION}, message.encode())

    def _add_cors_headers(self, headers: dict) -> dict:
        """Add CORS headers if cors_allow_origins is configured."""
        if self.cors_allow_origins:
            headers["Access-Control-Allow-Origin"] = self.cors_allow_origins
            headers["Access-Control-Expose-Headers"] = _TUS_EXPOSE_HEADERS
            headers["Access-Control-Allow-Methods"] = "GET,POST,HEAD,PATCH,DELETE,OPTIONS"
            headers["Access-Control-Allow-Headers"] = _TUS_ALLOW_HEADERS
        return headers

    def _format_expiry(self, expires_at: datetime) -> str:
        """Format expiry datetime as RFC 7231 date string."""
        return formatdate(expires_at.timestamp(), usegmt=True)

    def handle_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes = b""
    ) -> tuple[int, dict[str, str], bytes]:
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

        # Check TUS version (required by TUS spec for all non-OPTIONS requests)
        if method != "OPTIONS":
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
        elif method == "POST" and path == self.base_path:
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
                result = self._handle_patch(upload_id, headers, body)
        elif method == "DELETE" and path.startswith(self.base_path + "/"):
            upload_id = path[len(self.base_path) + 1 :]
            if not self._validate_upload_id(upload_id):
                result = self._error_response(400, "Invalid upload ID format")
            else:
                result = self._handle_delete(upload_id, headers)
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

    def _handle_options(
        self, path: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle OPTIONS request for capability discovery."""
        logger.debug("Handling OPTIONS request")
        response_headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Tus-Version": self.TUS_VERSION,
            "Tus-Extension": ",".join(self.SUPPORTED_EXTENSIONS),
            "Tus-Checksum-Algorithm": "sha1",
        }

        if self.max_size > 0:
            response_headers["Tus-Max-Size"] = str(self.max_size)

        return (204, response_headers, b"")

    def _handle_create(
        self, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle POST request to create a new upload."""
        # Concatenation extension routing:
        #   Upload-Concat: partial     -> create a partial upload
        #   Upload-Concat: final;<...> -> create a final upload that merges partials
        concat_header = headers.get("upload-concat", "").strip()
        if concat_header.startswith("final"):
            return self._handle_create_final(concat_header, headers)
        is_partial = concat_header == "partial"

        upload_length_str = headers.get("upload-length")
        if not upload_length_str:
            logger.error("Missing Upload-Length header")
            return self._error_response(400, "Missing Upload-Length header")

        try:
            upload_length = int(upload_length_str)
        except ValueError:
            logger.error("Invalid Upload-Length header: %s", upload_length_str)
            return self._error_response(400, "Invalid Upload-Length header")

        if upload_length < 0:
            logger.error("Negative Upload-Length header: %s", upload_length)
            return self._error_response(400, "Upload-Length must not be negative")

        if self.max_size > 0 and upload_length > self.max_size:
            logger.warning("Upload size %s exceeds maximum %s", upload_length, self.max_size)
            return self._error_response(413, "Upload exceeds maximum size")

        # Parse metadata
        metadata, err = self._parse_metadata(headers.get("upload-metadata", ""))
        if metadata is None:
            return self._error_response(400, err)

        # Generate upload ID
        upload_id = str(uuid.uuid4())

        # Invoke on_upload_create hook (may modify metadata or reject)
        if self._on_upload_create:
            try:
                result = self._invoke_pre_hook(
                    self._on_upload_create,
                    upload_id,
                    metadata,
                    upload_length,
                )
                if isinstance(result, dict):
                    metadata = result
            except TusHookError as e:
                return (
                    e.status_code,
                    {"Tus-Resumable": self.TUS_VERSION},
                    e.body.encode(),
                )

        # Compute expiry
        expires_at = None
        if self.upload_expiry is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.upload_expiry)

        # Create upload
        self.storage.create_upload(
            upload_id, upload_length, metadata, expires_at, is_partial=is_partial
        )
        if self._metrics is not None:
            self._metrics.inc("tusd_uploads_created_total")
        logger.info(
            "Created upload %s with length %s, metadata: %s",
            upload_id,
            upload_length,
            metadata,
        )

        # Handle creation-with-upload: process initial data if provided
        initial_offset = 0
        content_type = headers.get("content-type", "")
        if body and content_type != "application/offset+octet-stream":
            # Body present but Content-Type doesn't match — body is silently ignored per TUS spec.
            # Log a warning so developers can catch misconfigurations.
            logger.warning(
                "POST body received with Content-Type '%s' instead of "
                "application/offset+octet-stream; body ignored (not creation-with-upload)",
                content_type,
            )
        if body and content_type == "application/offset+octet-stream":
            self.storage.write_chunk(upload_id, 0, body)
            initial_offset = len(body)
            self.storage.update_offset(upload_id, initial_offset)
            logger.info("creation-with-upload: wrote %s bytes for %s", initial_offset, upload_id)

        # Handle upload completion (zero-length upload or creation-with-upload)
        if initial_offset >= upload_length and self.storage.complete_upload(upload_id):
            if self._metrics is not None:
                self._metrics.inc("tusd_uploads_finished_total")
            if self._on_upload_complete:
                file_info = self.storage.get_file_info(upload_id)
                self._invoke_post_hook(
                    self._on_upload_complete,
                    upload_id,
                    metadata,
                    file_info,
                )

        # Return response
        response_headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Location": f"{self.base_path}/{upload_id}",
            "Upload-Offset": str(initial_offset),
        }

        if expires_at:
            response_headers["Upload-Expires"] = self._format_expiry(expires_at)

        return (201, response_headers, b"")

    def _parse_metadata(self, upload_metadata: str) -> tuple[Optional[dict[str, str]], str]:
        """Parse the Upload-Metadata header value.

        Returns (metadata_dict, None) on success, or (None, error_message) on failure.
        """
        metadata: dict[str, str] = {}
        if not upload_metadata:
            return metadata, ""
        if len(upload_metadata) > self._MAX_METADATA_SIZE:
            return None, (
                f"Upload-Metadata exceeds maximum size of {self._MAX_METADATA_SIZE} bytes"
            )
        for pair in upload_metadata.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if " " in pair:
                key, value = pair.split(" ", 1)
                try:
                    metadata[key] = base64.b64decode(value).decode("utf-8")
                except (ValueError, UnicodeDecodeError, binascii.Error) as e:
                    return None, f"Invalid base64 encoding for metadata key '{key}': {e}"
            else:
                metadata[pair] = ""
        return metadata, ""

    def _handle_create_final(
        self, concat_header: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle POST with `Upload-Concat: final;<space-separated upload URLs>`."""
        try:
            _, urls_part = concat_header.split(";", 1)
        except ValueError:
            return self._error_response(400, "Invalid Upload-Concat header")

        prefix = self.base_path + "/"
        partial_ids: list[str] = []
        for raw_url in urls_part.split():
            raw_url = raw_url.strip()
            if not raw_url:
                continue
            # Accept absolute URLs; extract the path portion.
            path = raw_url
            if "://" in raw_url:
                from urllib.parse import urlparse

                path = urlparse(raw_url).path
            if not path.startswith(prefix):
                return self._error_response(
                    400, f"Upload-Concat references unknown upload: {raw_url}"
                )
            upload_id = path[len(prefix) :]
            if not self._validate_upload_id(upload_id):
                return self._error_response(400, "Invalid upload ID in Upload-Concat")
            partial_ids.append(upload_id)

        if not partial_ids:
            return self._error_response(400, "Upload-Concat final requires partial URLs")

        metadata, err = self._parse_metadata(headers.get("upload-metadata", ""))
        if metadata is None:
            return self._error_response(400, err)

        final_id = str(uuid.uuid4())
        try:
            total_length = self.storage.concatenate_uploads(final_id, partial_ids, metadata)
        except ValueError as e:
            return self._error_response(400, str(e))
        except NotImplementedError as e:
            return self._error_response(501, str(e))

        if self.max_size > 0 and total_length > self.max_size:
            # Concatenated payload exceeds limit — delete and reject.
            self.storage.delete_upload(final_id)
            return self._error_response(413, "Concatenated upload exceeds maximum size")

        logger.info(
            "Created final upload %s by concatenating %s partials (total %s bytes)",
            final_id,
            len(partial_ids),
            total_length,
        )

        if self._metrics is not None:
            self._metrics.inc("tusd_uploads_finished_total")
        if self._on_upload_complete:
            file_info = self.storage.get_file_info(final_id)
            self._invoke_post_hook(self._on_upload_complete, final_id, metadata, file_info)

        response_headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Location": f"{self.base_path}/{final_id}",
            "Upload-Offset": str(total_length),
            "Upload-Length": str(total_length),
        }
        return (201, response_headers, b"")

    def _handle_head(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle HEAD request to get upload offset."""
        upload = self.storage.get_upload(upload_id)
        if not upload:
            logger.warning("Upload not found: %s", upload_id)
            return self._error_response(404, "Upload not found")

        # Check expiration
        expires_at = upload.get("expires_at")
        if expires_at and expires_at < datetime.now(timezone.utc):
            logger.warning("Upload expired: %s", upload_id)
            return self._error_response(410, "Upload has expired")

        logger.debug(
            "HEAD request for upload %s: offset=%s, length=%s",
            upload_id,
            upload["offset"],
            upload["upload_length"],
        )
        response_headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Upload-Offset": str(upload["offset"]),
            "Upload-Length": str(upload["upload_length"]),
            "Cache-Control": "no-store",
        }

        if expires_at:
            response_headers["Upload-Expires"] = self._format_expiry(expires_at)

        # Include metadata if present
        metadata = upload.get("metadata", {})
        if metadata:
            encoded_metadata = []
            for key, value in metadata.items():
                value_bytes = value.encode("utf-8")
                encoded_value = base64.b64encode(value_bytes).decode("ascii")
                encoded_metadata.append(f"{key} {encoded_value}")
            response_headers["Upload-Metadata"] = ",".join(encoded_metadata)

        return (200, response_headers, b"")

    def _handle_patch(
        self, upload_id: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle PATCH request to append data to upload."""
        upload = self.storage.get_upload(upload_id)
        if not upload:
            logger.warning("Upload not found: %s", upload_id)
            return self._error_response(404, "Upload not found")

        # Check expiration
        expires_at = upload.get("expires_at")
        if expires_at and expires_at < datetime.now(timezone.utc):
            logger.warning("Upload expired: %s", upload_id)
            return self._error_response(410, "Upload has expired")

        # Check if already completed
        if upload.get("completed"):
            logger.warning("Upload already completed: %s", upload_id)
            return self._error_response(403, "Upload already completed")

        # Check content type
        content_type = headers.get("content-type", "")
        if content_type != "application/offset+octet-stream":
            logger.error("Invalid Content-Type: %s", content_type)
            return self._error_response(415, "Invalid Content-Type")

        # Check upload offset
        upload_offset_str = headers.get("upload-offset")
        if not upload_offset_str:
            logger.error("Missing Upload-Offset header")
            return self._error_response(400, "Missing Upload-Offset header")

        try:
            upload_offset = int(upload_offset_str)
        except ValueError:
            logger.error("Invalid Upload-Offset header: %s", upload_offset_str)
            return self._error_response(400, "Invalid Upload-Offset header")

        if upload_offset < 0:
            logger.error("Negative Upload-Offset: %s", upload_offset)
            return self._error_response(400, "Upload-Offset must not be negative")

        if upload_offset != upload["offset"]:
            logger.error(
                "Upload-Offset mismatch: expected %s, got %s", upload["offset"], upload_offset
            )
            return self._error_response(409, "Upload-Offset mismatch")

        # Verify checksum if provided
        upload_checksum = headers.get("upload-checksum")
        if upload_checksum:
            try:
                algo, checksum = upload_checksum.split(" ", 1)
                if algo == "sha1":
                    computed = hashlib.sha1(body).hexdigest()
                    provided = base64.b64decode(checksum).hex()
                    if computed != provided:
                        logger.error("Checksum mismatch for upload %s", upload_id)
                        return self._error_response(460, "Checksum mismatch")
                else:
                    logger.error("Unsupported checksum algorithm: %s", algo)
                    return self._error_response(400, f"Unsupported checksum algorithm: {algo}")
            except (ValueError, binascii.Error) as e:
                logger.error("Invalid Upload-Checksum header: %s", e)
                return self._error_response(400, "Invalid Upload-Checksum header")

        # Reject chunk if it would exceed the declared upload length
        new_offset = upload_offset + len(body)
        if new_offset > upload["upload_length"]:
            logger.error(
                "Chunk exceeds upload length: %s > %s", new_offset, upload["upload_length"]
            )
            return self._error_response(400, "Chunk would exceed declared upload length")

        # Reject chunk if it exceeds max_chunk_size
        if self.max_chunk_size > 0 and len(body) > self.max_chunk_size:
            logger.warning(
                "Chunk size %s exceeds max_chunk_size %s", len(body), self.max_chunk_size
            )
            return self._error_response(413, "Chunk exceeds maximum chunk size")

        # Write chunk then atomically advance offset.
        # If another concurrent request already advanced the offset, return 409.
        self.storage.write_chunk(upload_id, upload_offset, body)
        if not self.storage.update_offset_atomic(upload_id, upload_offset, new_offset):
            return self._error_response(409, "Concurrent write conflict; use HEAD to re-sync")

        if self._metrics is not None:
            self._metrics.inc("tusd_bytes_received_total", value=len(body))

        logger.info(
            "PATCH upload %s: wrote %s bytes, new offset: %s/%s",
            upload_id,
            len(body),
            new_offset,
            upload["upload_length"],
        )

        # Finalize storage and fire on_upload_complete if this PATCH completed the upload
        if new_offset >= upload["upload_length"] and self.storage.complete_upload(upload_id):
            if self._metrics is not None:
                self._metrics.inc("tusd_uploads_finished_total")
            if self._on_upload_complete:
                file_info = self.storage.get_file_info(upload_id)
                self._invoke_post_hook(
                    self._on_upload_complete,
                    upload_id,
                    upload.get("metadata", {}),
                    file_info,
                )

        # Return response
        response_headers = {
            "Tus-Resumable": self.TUS_VERSION,
            "Upload-Offset": str(new_offset),
        }

        if expires_at:
            response_headers["Upload-Expires"] = self._format_expiry(expires_at)

        return (204, response_headers, b"")

    def _handle_delete(
        self, upload_id: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle DELETE request to terminate upload."""
        upload = self.storage.get_upload(upload_id)
        if not upload:
            logger.warning("Upload not found for deletion: %s", upload_id)
            return self._error_response(404, "Upload not found")

        self.storage.delete_upload(upload_id)
        logger.info("Deleted upload %s", upload_id)
        if self._metrics is not None:
            self._metrics.inc("tusd_uploads_terminated_total")

        # Fire on_upload_terminate after successful deletion
        if self._on_upload_terminate:
            self._invoke_post_hook(self._on_upload_terminate, upload_id)

        response_headers = {
            "Tus-Resumable": self.TUS_VERSION,
        }

        return (204, response_headers, b"")


class TusHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for TUS server."""

    tus_server: Optional[TusServer] = None

    def do_OPTIONS(self) -> None:
        """Handle OPTIONS request."""
        self._handle_request("OPTIONS")

    def do_GET(self) -> None:
        """Serve /metrics when a metrics registry is attached; otherwise 404."""
        if (
            self.tus_server is not None
            and self.tus_server._metrics is not None
            and self.path == self.tus_server._metrics_path
        ):
            body = self.tus_server._metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        """Handle POST request."""
        self._handle_request("POST")

    def do_HEAD(self) -> None:
        """Handle HEAD request."""
        self._handle_request("HEAD")

    def do_PATCH(self) -> None:
        """Handle PATCH request."""
        self._handle_request("PATCH")

    def do_DELETE(self) -> None:
        """Handle DELETE request."""
        self._handle_request("DELETE")

    def setup(self) -> None:
        """Set socket read timeout from server config to guard against Slowloris."""
        super().setup()
        if self.tus_server and self.tus_server.request_timeout > 0:
            self.connection.settimeout(self.tus_server.request_timeout)

    def _handle_request(self, method: str) -> None:
        """Handle incoming request."""
        if self.tus_server is None:
            self.send_response(500)
            self.end_headers()
            return
        # Read body for POST/PATCH
        body = b""
        if method in ("POST", "PATCH"):
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (ValueError, TypeError):
                self.send_response(400)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Invalid Content-Length header")
                return
            if content_length < 0:
                self.send_response(400)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Content-Length must not be negative")
                return
            max_size = self.tus_server.max_size
            if max_size > 0 and content_length > max_size:
                self.send_response(413)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Request entity too large")
                return
            max_chunk = self.tus_server.max_chunk_size
            if method == "PATCH" and max_chunk > 0 and content_length > max_chunk:
                self.send_response(413)
                self.send_header("Tus-Resumable", self.tus_server.TUS_VERSION)
                self.end_headers()
                self.wfile.write(b"Chunk exceeds maximum chunk size")
                return
            if content_length > 0:
                body = self.rfile.read(content_length)

        # Convert headers to dict
        headers = dict(self.headers)

        # Handle request
        status, response_headers, response_body = self.tus_server.handle_request(
            method, self.path, headers, body
        )

        # Send response
        self.send_response(status)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging."""
        pass
