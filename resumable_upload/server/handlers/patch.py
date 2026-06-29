"""PATCH handler — append a chunk to an in-progress upload."""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from resumable_upload.server.headers import format_expiry

if TYPE_CHECKING:
    from resumable_upload.server.core import TusServerCore

logger = logging.getLogger(__name__)


@dataclass
class _PatchPlan:
    """Validated decision for a PATCH, produced without touching storage I/O.

    The sync and async handlers run :func:`_plan_patch` to apply every TUS
    protocol rule (offset, checksum, defer-length, size limits) in one place,
    then perform their respective sync/async I/O against this plan.
    """

    expected_offset: int
    new_offset: int
    effective_length: int
    commit_length: int | None
    expires_at: datetime | None


def _plan_patch(
    server: TusServerCore,
    upload_id: str,
    upload: dict,
    headers: dict[str, str],
    body: bytes,
) -> _PatchPlan | tuple[int, dict[str, str], bytes]:
    """Validate a PATCH request against the TUS protocol rules (no I/O).

    Returns a :class:`_PatchPlan` describing the writes to perform, or an
    error response tuple if any rule is violated.
    """
    expires_at = upload.get("expires_at")
    if expires_at and expires_at < datetime.now(timezone.utc):
        logger.warning("Upload expired: %s", upload_id)
        return server._error_response(410, "Upload has expired")

    if upload.get("completed"):
        logger.warning("Upload already completed: %s", upload_id)
        return server._error_response(403, "Upload already completed")

    content_type = headers.get("content-type", "")
    if content_type != "application/offset+octet-stream":
        logger.error("Invalid Content-Type: %s", content_type)
        return server._error_response(415, "Invalid Content-Type")

    # Upload-Defer-Length: commit the final length on the first PATCH.
    commit_length: int | None = None
    effective_length = upload["upload_length"]
    patch_upload_length = headers.get("upload-length")
    if effective_length is None:
        if not patch_upload_length:
            return server._error_response(
                400, "Upload-Length header required for deferred-length PATCH"
            )
        try:
            commit_length = int(patch_upload_length)
        except ValueError:
            return server._error_response(400, "Invalid Upload-Length header")
        if commit_length < 0:
            return server._error_response(400, "Upload-Length must not be negative")
        if server.max_size > 0 and commit_length > server.max_size:
            return server._error_response(413, "Upload exceeds maximum size")
        effective_length = commit_length
    elif patch_upload_length is not None:
        # Length already committed: reject attempts to change it.
        try:
            resent_length = int(patch_upload_length)
        except ValueError:
            return server._error_response(400, "Invalid Upload-Length header")
        if resent_length != effective_length:
            return server._error_response(
                400, "Upload-Length already committed and cannot be changed"
            )

    upload_offset_str = headers.get("upload-offset")
    if not upload_offset_str:
        logger.error("Missing Upload-Offset header")
        return server._error_response(400, "Missing Upload-Offset header")

    try:
        upload_offset = int(upload_offset_str)
    except ValueError:
        logger.error("Invalid Upload-Offset header: %s", upload_offset_str)
        return server._error_response(400, "Invalid Upload-Offset header")

    if upload_offset < 0:
        logger.error("Negative Upload-Offset: %s", upload_offset)
        return server._error_response(400, "Upload-Offset must not be negative")

    if upload_offset != upload["offset"]:
        logger.error(
            "Upload-Offset mismatch: expected %s, got %s",
            upload["offset"],
            upload_offset,
        )
        return server._error_response(409, "Upload-Offset mismatch")

    upload_checksum = headers.get("upload-checksum")
    if upload_checksum:
        try:
            algo, checksum = upload_checksum.split(" ", 1)
        except ValueError:
            return server._error_response(400, "Invalid Upload-Checksum header")
        if not server._checksums.is_supported(algo):
            logger.error("Unsupported checksum algorithm: %s", algo)
            return server._error_response(400, f"Unsupported checksum algorithm: {algo}")
        try:
            provided = base64.b64decode(checksum).hex()
        except (binascii.Error, ValueError) as e:
            logger.error("Invalid Upload-Checksum header: %s", e)
            return server._error_response(400, "Invalid Upload-Checksum header")
        computed = server._checksums.compute(algo, body)
        if computed != provided:
            logger.error("Checksum mismatch for upload %s", upload_id)
            return server._error_response(460, "Checksum mismatch")

    new_offset = upload_offset + len(body)
    if new_offset > effective_length:
        logger.error(
            "Chunk exceeds upload length: %s > %s",
            new_offset,
            effective_length,
        )
        return server._error_response(400, "Chunk would exceed declared upload length")

    if server.max_chunk_size > 0 and len(body) > server.max_chunk_size:
        logger.warning(
            "Chunk size %s exceeds max_chunk_size %s",
            len(body),
            server.max_chunk_size,
        )
        return server._error_response(413, "Chunk exceeds maximum chunk size")

    return _PatchPlan(
        expected_offset=upload_offset,
        new_offset=new_offset,
        effective_length=effective_length,
        commit_length=commit_length,
        expires_at=expires_at,
    )


def _patch_response(plan: _PatchPlan, server: TusServerCore) -> tuple[int, dict[str, str], bytes]:
    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Upload-Offset": str(plan.new_offset),
    }
    if plan.expires_at:
        response_headers["Upload-Expires"] = format_expiry(plan.expires_at)
    return (204, response_headers, b"")


def handle_patch(
    server: TusServerCore,
    upload_id: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    upload = server.storage.get_upload(upload_id)
    if not upload:
        logger.warning("Upload not found: %s", upload_id)
        return server._error_response(404, "Upload not found")

    plan = _plan_patch(server, upload_id, upload, headers, body)
    if not isinstance(plan, _PatchPlan):
        return plan

    if plan.commit_length is not None:
        server.storage.set_upload_length(upload_id, plan.commit_length)

    # Write chunk then atomically advance offset. If another concurrent request
    # already advanced the offset, return 409.
    server.storage.write_chunk(upload_id, plan.expected_offset, body)
    if not server.storage.update_offset_atomic(upload_id, plan.expected_offset, plan.new_offset):
        return server._error_response(409, "Concurrent write conflict; use HEAD to re-sync")

    if server._metrics is not None:
        server._metrics.inc("tusd_bytes_received_total", value=len(body))

    logger.info(
        "PATCH upload %s: wrote %s bytes, new offset: %s/%s",
        upload_id,
        len(body),
        plan.new_offset,
        plan.effective_length,
    )

    if plan.new_offset >= plan.effective_length and server.storage.complete_upload(upload_id):
        if server._metrics is not None:
            server._metrics.inc("tusd_uploads_finished_total")
        if server._on_upload_complete:
            file_info = server.storage.get_file_info(upload_id)
            server._invoke_post_hook(
                server._on_upload_complete,
                upload_id,
                upload.get("metadata", {}),
                file_info,
            )

    return _patch_response(plan, server)


async def handle_patch_async(
    server: TusServerCore,
    upload_id: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    """Async sibling of :func:`handle_patch`.

    Shares :func:`_plan_patch` for all protocol validation; only the storage
    I/O is awaited so true-async backends never block the event loop.
    """
    upload = await server.storage.get_upload_async(upload_id)
    if not upload:
        logger.warning("Upload not found: %s", upload_id)
        return server._error_response(404, "Upload not found")

    plan = _plan_patch(server, upload_id, upload, headers, body)
    if not isinstance(plan, _PatchPlan):
        return plan

    if plan.commit_length is not None:
        await server.storage.set_upload_length_async(upload_id, plan.commit_length)

    await server.storage.write_chunk_async(upload_id, plan.expected_offset, body)
    if not await server.storage.update_offset_atomic_async(
        upload_id, plan.expected_offset, plan.new_offset
    ):
        return server._error_response(409, "Concurrent write conflict; use HEAD to re-sync")

    if server._metrics is not None:
        server._metrics.inc("tusd_bytes_received_total", value=len(body))

    logger.info(
        "PATCH upload %s: wrote %s bytes, new offset: %s/%s",
        upload_id,
        len(body),
        plan.new_offset,
        plan.effective_length,
    )

    if plan.new_offset >= plan.effective_length and await server.storage.complete_upload_async(
        upload_id
    ):
        if server._metrics is not None:
            server._metrics.inc("tusd_uploads_finished_total")
        if server._on_upload_complete:
            file_info = server.storage.get_file_info(upload_id)
            server._invoke_post_hook(
                server._on_upload_complete,
                upload_id,
                upload.get("metadata", {}),
                file_info,
            )

    return _patch_response(plan, server)
