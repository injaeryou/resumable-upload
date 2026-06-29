"""HEAD handler — return current upload offset and metadata."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from resumable_upload.server.headers import format_expiry

if TYPE_CHECKING:
    from resumable_upload.server.core import TusServerCore

logger = logging.getLogger(__name__)


def handle_head(
    server: TusServerCore, upload_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    upload = server.storage.get_upload(upload_id)
    return _build_head_response(server, upload_id, upload)


async def handle_head_async(
    server: TusServerCore, upload_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    upload = await server.storage.get_upload_async(upload_id)
    return _build_head_response(server, upload_id, upload)


def _build_head_response(
    server: TusServerCore, upload_id: str, upload: dict | None
) -> tuple[int, dict[str, str], bytes]:
    if not upload:
        logger.warning("Upload not found: %s", upload_id)
        return server._error_response(404, "Upload not found")

    expires_at = upload.get("expires_at")
    if expires_at and expires_at < datetime.now(timezone.utc):
        logger.warning("Upload expired: %s", upload_id)
        return server._error_response(410, "Upload has expired")

    logger.debug(
        "HEAD request for upload %s: offset=%s, length=%s",
        upload_id,
        upload["offset"],
        upload["upload_length"],
    )
    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Upload-Offset": str(upload["offset"]),
        "Cache-Control": "no-store",
    }
    if upload["upload_length"] is None:
        # Upload-Defer-Length extension: length not yet committed.
        response_headers["Upload-Defer-Length"] = "1"
    else:
        response_headers["Upload-Length"] = str(upload["upload_length"])

    if expires_at:
        response_headers["Upload-Expires"] = format_expiry(expires_at)

    metadata = upload.get("metadata", {})
    if metadata:
        encoded_metadata = []
        for key, value in metadata.items():
            value_bytes = value.encode("utf-8")
            encoded_value = base64.b64encode(value_bytes).decode("ascii")
            encoded_metadata.append(f"{key} {encoded_value}")
        response_headers["Upload-Metadata"] = ",".join(encoded_metadata)

    # Concatenation extension: a partial upload's HEAD response must advertise
    # Upload-Concat so a spec-conformant client can tell it apart from a normal
    # upload. (Final uploads complete synchronously on POST and are never
    # observable as an in-progress concat here, so only "partial" is echoed.)
    if upload.get("is_partial"):
        response_headers["Upload-Concat"] = "partial"

    return (200, response_headers, b"")
