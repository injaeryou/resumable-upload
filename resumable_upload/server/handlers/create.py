"""POST handler — create a new upload, including ``Upload-Concat: final``."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
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


@dataclass
class _CreatePlan:
    """Validated decision for a plain POST creation (no I/O performed)."""

    upload_id: str
    upload_length: int | None
    metadata: dict
    expires_at: datetime | None
    is_partial: bool
    write_body: bool  # creation-with-upload: body present with the offset+octet-stream type


@dataclass
class _CreateFinalPlan:
    """Validated decision for ``Upload-Concat: final`` (no I/O performed)."""

    final_id: str
    partial_ids: list[str]
    metadata: dict
    expires_at: datetime | None


def _plan_create(
    server: TusServerCore, headers: dict[str, str], body: bytes
) -> _CreatePlan | tuple[int, dict[str, str], bytes]:
    """Validate a plain (non-final) POST creation request (no I/O).

    Includes the synchronous ``on_upload_create`` pre-hook, which both the
    sync and async handlers invoke identically.
    """
    is_partial = headers.get("upload-concat", "").strip() == "partial"

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

    content_type = headers.get("content-type", "")
    write_body = bool(body) and content_type == "application/offset+octet-stream"
    if body and not write_body:
        # Body present but Content-Type doesn't match — body silently ignored per
        # the TUS spec. Log a warning so developers can catch misconfigurations.
        logger.warning(
            "POST body received with Content-Type '%s' instead of "
            "application/offset+octet-stream; body ignored (not creation-with-upload)",
            content_type,
        )

    return _CreatePlan(
        upload_id=upload_id,
        upload_length=upload_length,
        metadata=metadata,
        expires_at=expires_at,
        is_partial=is_partial,
        write_body=write_body,
    )


def _plan_create_final(
    server: TusServerCore, concat_header: str, headers: dict[str, str]
) -> _CreateFinalPlan | tuple[int, dict[str, str], bytes]:
    """Validate an ``Upload-Concat: final;<urls>`` request (no I/O)."""
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

    return _CreateFinalPlan(
        final_id=final_id,
        partial_ids=partial_ids,
        metadata=metadata,
        expires_at=expires_at,
    )


def _create_response(
    plan: _CreatePlan, initial_offset: int, server: TusServerCore, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Location": server._build_location(plan.upload_id, headers),
        "Upload-Offset": str(initial_offset),
    }
    if plan.expires_at:
        response_headers["Upload-Expires"] = format_expiry(plan.expires_at)
    return (201, response_headers, b"")


def _create_final_response(
    plan: _CreateFinalPlan,
    total_length: int | None,
    server: TusServerCore,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Location": server._build_location(plan.final_id, headers),
    }
    if total_length is not None:
        # Assembled synchronously; a pending (unfinished) final has no known
        # offset/length yet, so those headers are omitted.
        response_headers["Upload-Offset"] = str(total_length)
        response_headers["Upload-Length"] = str(total_length)
    if plan.expires_at:
        response_headers["Upload-Expires"] = format_expiry(plan.expires_at)
    return (201, response_headers, b"")


def _final_concat_kwargs(server: TusServerCore, plan: _CreateFinalPlan) -> dict:
    """kwargs for Storage.concatenate_uploads, passed only when needed.

    Optional kwargs are omitted when inactive so third-party Storage
    subclasses predating them keep working (mirrors the is_partial pattern
    in handle_create).
    """
    kwargs: dict = {}
    if plan.expires_at is not None:
        kwargs["expires_at"] = plan.expires_at
    if getattr(server.storage, "supports_unfinished_concat", False):
        kwargs["allow_unfinished"] = True
    return kwargs


def handle_create(
    server: TusServerCore, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    # Concatenation extension routing:
    #   Upload-Concat: partial     -> create a partial upload
    #   Upload-Concat: final;<...> -> create a final upload that merges partials
    concat_header = headers.get("upload-concat", "").strip()
    if concat_header and server.disable_concatenation:
        return server._error_response(400, "Concatenation extension is disabled")
    if concat_header.startswith("final"):
        return handle_create_final(server, concat_header, headers)

    plan = _plan_create(server, headers, body)
    if not isinstance(plan, _CreatePlan):
        return plan

    # Only pass is_partial when True so third-party Storage subclasses predating
    # the concatenation extension don't need to accept the new kwarg.
    create_kwargs: dict = {}
    if plan.is_partial:
        create_kwargs["is_partial"] = True
    server.storage.create_upload(
        plan.upload_id, plan.upload_length, plan.metadata, plan.expires_at, **create_kwargs
    )
    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_created_total")
    logger.info(
        "Created upload %s with length %s, metadata: %s",
        plan.upload_id,
        plan.upload_length,
        plan.metadata,
    )

    initial_offset = 0
    if plan.write_body:
        server.storage.write_chunk(plan.upload_id, 0, body)
        initial_offset = len(body)
        server.storage.update_offset(plan.upload_id, initial_offset)
        logger.info("creation-with-upload: wrote %s bytes for %s", initial_offset, plan.upload_id)

    # Handle upload completion (zero-length upload or creation-with-upload).
    # Deferred-length uploads cannot complete here; their length is unknown.
    completion_result = None
    if (
        plan.upload_length is not None
        and initial_offset >= plan.upload_length
        and server.storage.complete_upload(plan.upload_id)
    ):
        if server._metrics is not None:
            server._metrics.inc("tusd_uploads_finished_total")
        # Partials never fire on_upload_complete individually (parity with
        # the PATCH completion path); only the assembled final does.
        if server._on_upload_complete and not plan.is_partial:
            file_info = server.storage.get_file_info(plan.upload_id)
            completion_result = server._invoke_post_hook(
                server._on_upload_complete,
                plan.upload_id,
                plan.metadata,
                file_info,
            )

    return server._apply_completion_response(
        completion_result, _create_response(plan, initial_offset, server, headers)
    )


def handle_create_final(
    server: TusServerCore, concat_header: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """Handle POST with ``Upload-Concat: final;<space-separated upload URLs>``."""
    plan = _plan_create_final(server, concat_header, headers)
    if not isinstance(plan, _CreateFinalPlan):
        return plan

    # Enforce Tus-Max-Size on the declared partial lengths up front, so a
    # pending (unfinished) final that could never fit is rejected before any
    # row is created. Deferred-length partials have no knowable size here;
    # reject them outright rather than accept a final that assembly might have
    # to destroy later with no client request left to answer 413 to.
    if server.max_size > 0:
        declared = 0
        for pid in plan.partial_ids:
            p = server.storage.get_upload(pid)
            if p and p.get("upload_length") is None:
                return server._error_response(
                    400,
                    "Cannot enforce Tus-Max-Size on a final over deferred-length partials",
                )
            if p and p.get("upload_length") is not None:
                declared += p["upload_length"]
        if declared > server.max_size:
            return server._error_response(413, "Concatenated upload exceeds maximum size")

    try:
        total_length = server.storage.concatenate_uploads(
            plan.final_id, plan.partial_ids, plan.metadata, **_final_concat_kwargs(server, plan)
        )
    except ValueError as e:
        return server._error_response(400, str(e))
    except NotImplementedError as e:
        return server._error_response(501, str(e))

    if total_length is None:
        # concatenation-unfinished: final stays pending until its partials
        # complete; assembly (and on_upload_complete) happens in the PATCH
        # handler that finishes the last partial.
        logger.info(
            "Created pending final upload %s over %s unfinished partials",
            plan.final_id,
            len(plan.partial_ids),
        )
        return _create_final_response(plan, None, server, headers)

    if server.max_size > 0 and total_length > server.max_size:
        # Concatenated payload exceeds limit — delete and reject.
        server.storage.delete_upload(plan.final_id)
        return server._error_response(413, "Concatenated upload exceeds maximum size")

    logger.info(
        "Created final upload %s by concatenating %s partials (total %s bytes)",
        plan.final_id,
        len(plan.partial_ids),
        total_length,
    )

    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_finished_total")
    if server._on_upload_complete:
        file_info = server.storage.get_file_info(plan.final_id)
        server._invoke_post_hook(
            server._on_upload_complete, plan.final_id, plan.metadata, file_info
        )

    return _create_final_response(plan, total_length, server, headers)


# ---------------------------------------------------------------------------
# Async siblings — share the same planners; only storage I/O is awaited.
# ---------------------------------------------------------------------------


async def handle_create_async(
    server: TusServerCore, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    """Async sibling of :func:`handle_create`."""
    concat_header = headers.get("upload-concat", "").strip()
    if concat_header and server.disable_concatenation:
        return server._error_response(400, "Concatenation extension is disabled")
    if concat_header.startswith("final"):
        return await handle_create_final_async(server, concat_header, headers)

    plan = _plan_create(server, headers, body)
    if not isinstance(plan, _CreatePlan):
        return plan

    create_kwargs: dict = {}
    if plan.is_partial:
        create_kwargs["is_partial"] = True
    await server.storage.create_upload_async(
        plan.upload_id, plan.upload_length, plan.metadata, plan.expires_at, **create_kwargs
    )
    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_created_total")
    logger.info(
        "Created upload %s with length %s, metadata: %s",
        plan.upload_id,
        plan.upload_length,
        plan.metadata,
    )

    initial_offset = 0
    if plan.write_body:
        await server.storage.write_chunk_async(plan.upload_id, 0, body)
        initial_offset = len(body)
        await server.storage.update_offset_async(plan.upload_id, initial_offset)
        logger.info("creation-with-upload: wrote %s bytes for %s", initial_offset, plan.upload_id)

    completion_result = None
    if (
        plan.upload_length is not None
        and initial_offset >= plan.upload_length
        and await server.storage.complete_upload_async(plan.upload_id)
    ):
        if server._metrics is not None:
            server._metrics.inc("tusd_uploads_finished_total")
        # Partials never fire on_upload_complete individually (parity with
        # the PATCH completion path); only the assembled final does.
        if server._on_upload_complete and not plan.is_partial:
            file_info = server.storage.get_file_info(plan.upload_id)
            completion_result = server._invoke_post_hook(
                server._on_upload_complete,
                plan.upload_id,
                plan.metadata,
                file_info,
            )

    return server._apply_completion_response(
        completion_result, _create_response(plan, initial_offset, server, headers)
    )


async def handle_create_final_async(
    server: TusServerCore, concat_header: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """Async sibling of :func:`handle_create_final`."""
    plan = _plan_create_final(server, concat_header, headers)
    if not isinstance(plan, _CreateFinalPlan):
        return plan

    if server.max_size > 0:
        declared = 0
        for pid in plan.partial_ids:
            p = await server.storage.get_upload_async(pid)
            if p and p.get("upload_length") is not None:
                declared += p["upload_length"]
        if declared > server.max_size:
            return server._error_response(413, "Concatenated upload exceeds maximum size")

    try:
        total_length = await server.storage.concatenate_uploads_async(
            plan.final_id, plan.partial_ids, plan.metadata, **_final_concat_kwargs(server, plan)
        )
    except ValueError as e:
        return server._error_response(400, str(e))
    except NotImplementedError as e:
        return server._error_response(501, str(e))

    if total_length is None:
        logger.info(
            "Created pending final upload %s over %s unfinished partials",
            plan.final_id,
            len(plan.partial_ids),
        )
        return _create_final_response(plan, None, server, headers)

    if server.max_size > 0 and total_length > server.max_size:
        await server.storage.delete_upload_async(plan.final_id)
        return server._error_response(413, "Concatenated upload exceeds maximum size")

    logger.info(
        "Created final upload %s by concatenating %s partials (total %s bytes)",
        plan.final_id,
        len(plan.partial_ids),
        total_length,
    )

    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_finished_total")
    if server._on_upload_complete:
        file_info = server.storage.get_file_info(plan.final_id)
        server._invoke_post_hook(
            server._on_upload_complete, plan.final_id, plan.metadata, file_info
        )

    return _create_final_response(plan, total_length, server, headers)
