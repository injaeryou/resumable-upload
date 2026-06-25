"""POST handler — create a new upload, including ``Upload-Concat: final``."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from resumable_upload.exceptions import TusHookError
from resumable_upload.server.headers import (
    format_expiry,
    parse_metadata,
    validate_upload_id,
)

if TYPE_CHECKING:
    from resumable_upload.server.core import TusServerCore

logger = logging.getLogger(__name__)


def handle_create(
    server: TusServerCore, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    # Concatenation extension routing:
    #   Upload-Concat: partial     -> create a partial upload
    #   Upload-Concat: final;<...> -> create a final upload that merges partials
    concat_header = headers.get("upload-concat", "").strip()
    if concat_header.startswith("final"):
        return handle_create_final(server, concat_header, headers)
    is_partial = concat_header == "partial"

    defer_length = headers.get("upload-defer-length") == "1"
    upload_length_str = headers.get("upload-length")

    if defer_length and upload_length_str:
        return server._error_response(
            400, "Upload-Length and Upload-Defer-Length are mutually exclusive"
        )
    if not defer_length and not upload_length_str:
        logger.error("Missing Upload-Length header")
        return server._error_response(400, "Missing Upload-Length header")

    upload_length: int | None
    if defer_length:
        upload_length = None
    else:
        assert upload_length_str is not None  # guaranteed by the check above
        try:
            upload_length = int(upload_length_str)
        except ValueError:
            logger.error("Invalid Upload-Length header: %s", upload_length_str)
            return server._error_response(400, "Invalid Upload-Length header")

        if upload_length < 0:
            logger.error("Negative Upload-Length header: %s", upload_length)
            return server._error_response(400, "Upload-Length must not be negative")

        if server.max_size > 0 and upload_length > server.max_size:
            logger.warning("Upload size %s exceeds maximum %s", upload_length, server.max_size)
            return server._error_response(413, "Upload exceeds maximum size")

    metadata, err = parse_metadata(headers.get("upload-metadata", ""))
    if metadata is None:
        return server._error_response(400, err)

    upload_id = str(uuid.uuid4())

    if server._on_upload_create:
        try:
            result = server._invoke_pre_hook(
                server._on_upload_create,
                upload_id,
                metadata,
                upload_length,
            )
            if isinstance(result, dict):
                metadata = result
        except TusHookError as e:
            return (
                e.status_code,
                {"Tus-Resumable": server.TUS_VERSION},
                e.body.encode(),
            )

    expires_at = None
    if server.upload_expiry is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=server.upload_expiry)

    # Only pass is_partial when True so third-party Storage subclasses predating
    # the concatenation extension don't need to accept the new kwarg.
    create_kwargs: dict = {}
    if is_partial:
        create_kwargs["is_partial"] = True
    server.storage.create_upload(upload_id, upload_length, metadata, expires_at, **create_kwargs)
    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_created_total")
    logger.info(
        "Created upload %s with length %s, metadata: %s",
        upload_id,
        upload_length,
        metadata,
    )

    initial_offset = 0
    content_type = headers.get("content-type", "")
    if body and content_type != "application/offset+octet-stream":
        # Body present but Content-Type doesn't match — body silently ignored per
        # the TUS spec. Log a warning so developers can catch misconfigurations.
        logger.warning(
            "POST body received with Content-Type '%s' instead of "
            "application/offset+octet-stream; body ignored (not creation-with-upload)",
            content_type,
        )
    if body and content_type == "application/offset+octet-stream":
        server.storage.write_chunk(upload_id, 0, body)
        initial_offset = len(body)
        server.storage.update_offset(upload_id, initial_offset)
        logger.info("creation-with-upload: wrote %s bytes for %s", initial_offset, upload_id)

    # Handle upload completion (zero-length upload or creation-with-upload).
    # Deferred-length uploads cannot complete here; their length is unknown.
    if (
        upload_length is not None
        and initial_offset >= upload_length
        and server.storage.complete_upload(upload_id)
    ):
        if server._metrics is not None:
            server._metrics.inc("tusd_uploads_finished_total")
        if server._on_upload_complete:
            file_info = server.storage.get_file_info(upload_id)
            server._invoke_post_hook(
                server._on_upload_complete,
                upload_id,
                metadata,
                file_info,
            )

    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Location": f"{server.base_path}/{upload_id}",
        "Upload-Offset": str(initial_offset),
    }
    if expires_at:
        response_headers["Upload-Expires"] = format_expiry(expires_at)
    return (201, response_headers, b"")


def handle_create_final(
    server: TusServerCore, concat_header: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """Handle POST with ``Upload-Concat: final;<space-separated upload URLs>``."""
    try:
        _, urls_part = concat_header.split(";", 1)
    except ValueError:
        return server._error_response(400, "Invalid Upload-Concat header")

    prefix = server.base_path + "/"
    partial_ids: list[str] = []
    for raw_url in urls_part.split():
        raw_url = raw_url.strip()
        if not raw_url:
            continue
        # Accept absolute URLs; extract the path portion.
        path = raw_url
        if "://" in raw_url:
            path = urlparse(raw_url).path
        if not path.startswith(prefix):
            return server._error_response(
                400, f"Upload-Concat references unknown upload: {raw_url}"
            )
        upload_id = path[len(prefix) :]
        if not validate_upload_id(upload_id):
            return server._error_response(400, "Invalid upload ID in Upload-Concat")
        partial_ids.append(upload_id)

    if not partial_ids:
        return server._error_response(400, "Upload-Concat final requires partial URLs")

    metadata, err = parse_metadata(headers.get("upload-metadata", ""))
    if metadata is None:
        return server._error_response(400, err)

    final_id = str(uuid.uuid4())

    expires_at = None
    if server.upload_expiry is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=server.upload_expiry)

    # Only pass expires_at when set so third-party Storage subclasses predating
    # this fix don't need to adapt their concatenate_uploads signature
    # (mirrors the is_partial pattern in handle_create).
    concat_kwargs: dict = {}
    if expires_at is not None:
        concat_kwargs["expires_at"] = expires_at
    try:
        total_length = server.storage.concatenate_uploads(
            final_id, partial_ids, metadata, **concat_kwargs
        )
    except ValueError as e:
        return server._error_response(400, str(e))
    except NotImplementedError as e:
        return server._error_response(501, str(e))

    if server.max_size > 0 and total_length > server.max_size:
        # Concatenated payload exceeds limit — delete and reject.
        server.storage.delete_upload(final_id)
        return server._error_response(413, "Concatenated upload exceeds maximum size")

    logger.info(
        "Created final upload %s by concatenating %s partials (total %s bytes)",
        final_id,
        len(partial_ids),
        total_length,
    )

    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_finished_total")
    if server._on_upload_complete:
        file_info = server.storage.get_file_info(final_id)
        server._invoke_post_hook(server._on_upload_complete, final_id, metadata, file_info)

    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Location": f"{server.base_path}/{final_id}",
        "Upload-Offset": str(total_length),
        "Upload-Length": str(total_length),
    }
    if expires_at:
        response_headers["Upload-Expires"] = format_expiry(expires_at)
    return (201, response_headers, b"")


# ---------------------------------------------------------------------------
# Async siblings
# ---------------------------------------------------------------------------


async def handle_create_async(
    server: TusServerCore, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    """Async sibling of :func:`handle_create`."""
    concat_header = headers.get("upload-concat", "").strip()
    if concat_header.startswith("final"):
        return await handle_create_final_async(server, concat_header, headers)
    is_partial = concat_header == "partial"

    defer_length = headers.get("upload-defer-length") == "1"
    upload_length_str = headers.get("upload-length")

    if defer_length and upload_length_str:
        return server._error_response(
            400, "Upload-Length and Upload-Defer-Length are mutually exclusive"
        )
    if not defer_length and not upload_length_str:
        logger.error("Missing Upload-Length header")
        return server._error_response(400, "Missing Upload-Length header")

    upload_length: int | None
    if defer_length:
        upload_length = None
    else:
        assert upload_length_str is not None
        try:
            upload_length = int(upload_length_str)
        except ValueError:
            logger.error("Invalid Upload-Length header: %s", upload_length_str)
            return server._error_response(400, "Invalid Upload-Length header")
        if upload_length < 0:
            logger.error("Negative Upload-Length header: %s", upload_length)
            return server._error_response(400, "Upload-Length must not be negative")
        if server.max_size > 0 and upload_length > server.max_size:
            logger.warning("Upload size %s exceeds maximum %s", upload_length, server.max_size)
            return server._error_response(413, "Upload exceeds maximum size")

    metadata, err = parse_metadata(headers.get("upload-metadata", ""))
    if metadata is None:
        return server._error_response(400, err)

    upload_id = str(uuid.uuid4())

    if server._on_upload_create:
        try:
            result = server._invoke_pre_hook(
                server._on_upload_create,
                upload_id,
                metadata,
                upload_length,
            )
            if isinstance(result, dict):
                metadata = result
        except TusHookError as e:
            return (
                e.status_code,
                {"Tus-Resumable": server.TUS_VERSION},
                e.body.encode(),
            )

    expires_at = None
    if server.upload_expiry is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=server.upload_expiry)

    create_kwargs: dict = {}
    if is_partial:
        create_kwargs["is_partial"] = True
    await server.storage.create_upload_async(
        upload_id, upload_length, metadata, expires_at, **create_kwargs
    )
    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_created_total")
    logger.info(
        "Created upload %s with length %s, metadata: %s",
        upload_id,
        upload_length,
        metadata,
    )

    initial_offset = 0
    content_type = headers.get("content-type", "")
    if body and content_type != "application/offset+octet-stream":
        logger.warning(
            "POST body received with Content-Type '%s' instead of "
            "application/offset+octet-stream; body ignored (not creation-with-upload)",
            content_type,
        )
    if body and content_type == "application/offset+octet-stream":
        await server.storage.write_chunk_async(upload_id, 0, body)
        initial_offset = len(body)
        await server.storage.update_offset_async(upload_id, initial_offset)
        logger.info("creation-with-upload: wrote %s bytes for %s", initial_offset, upload_id)

    if (
        upload_length is not None
        and initial_offset >= upload_length
        and await server.storage.complete_upload_async(upload_id)
    ):
        if server._metrics is not None:
            server._metrics.inc("tusd_uploads_finished_total")
        if server._on_upload_complete:
            file_info = server.storage.get_file_info(upload_id)
            server._invoke_post_hook(
                server._on_upload_complete,
                upload_id,
                metadata,
                file_info,
            )

    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Location": f"{server.base_path}/{upload_id}",
        "Upload-Offset": str(initial_offset),
    }
    if expires_at:
        response_headers["Upload-Expires"] = format_expiry(expires_at)
    return (201, response_headers, b"")


async def handle_create_final_async(
    server: TusServerCore, concat_header: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """Async sibling of :func:`handle_create_final`."""
    try:
        _, urls_part = concat_header.split(";", 1)
    except ValueError:
        return server._error_response(400, "Invalid Upload-Concat header")

    prefix = server.base_path + "/"
    partial_ids: list[str] = []
    for raw_url in urls_part.split():
        raw_url = raw_url.strip()
        if not raw_url:
            continue
        path = raw_url
        if "://" in raw_url:
            path = urlparse(raw_url).path
        if not path.startswith(prefix):
            return server._error_response(
                400, f"Upload-Concat references unknown upload: {raw_url}"
            )
        upload_id = path[len(prefix) :]
        if not validate_upload_id(upload_id):
            return server._error_response(400, "Invalid upload ID in Upload-Concat")
        partial_ids.append(upload_id)

    if not partial_ids:
        return server._error_response(400, "Upload-Concat final requires partial URLs")

    metadata, err = parse_metadata(headers.get("upload-metadata", ""))
    if metadata is None:
        return server._error_response(400, err)

    final_id = str(uuid.uuid4())

    expires_at = None
    if server.upload_expiry is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=server.upload_expiry)

    concat_kwargs: dict = {}
    if expires_at is not None:
        concat_kwargs["expires_at"] = expires_at
    try:
        total_length = await server.storage.concatenate_uploads_async(
            final_id, partial_ids, metadata, **concat_kwargs
        )
    except ValueError as e:
        return server._error_response(400, str(e))
    except NotImplementedError as e:
        return server._error_response(501, str(e))

    if server.max_size > 0 and total_length > server.max_size:
        await server.storage.delete_upload_async(final_id)
        return server._error_response(413, "Concatenated upload exceeds maximum size")

    logger.info(
        "Created final upload %s by concatenating %s partials (total %s bytes)",
        final_id,
        len(partial_ids),
        total_length,
    )

    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_finished_total")
    if server._on_upload_complete:
        file_info = server.storage.get_file_info(final_id)
        server._invoke_post_hook(server._on_upload_complete, final_id, metadata, file_info)

    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Location": f"{server.base_path}/{final_id}",
        "Upload-Offset": str(total_length),
        "Upload-Length": str(total_length),
    }
    if expires_at:
        response_headers["Upload-Expires"] = format_expiry(expires_at)
    return (201, response_headers, b"")
