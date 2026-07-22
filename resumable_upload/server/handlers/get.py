"""GET handler — serve a completed upload's bytes (opt-in, non-standard).

tusd serves downloads by default; here ``TusServer(enable_downloads=True)``
opts in because the library is usually embedded next to framework routes.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import quote

if TYPE_CHECKING:
    from resumable_upload.server.core import TusServerCore

logger = logging.getLogger(__name__)

# Conservative RFC 6838-ish token/token pattern for Upload-Metadata "filetype".
_MIME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*/[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*$")


def _content_headers(metadata: dict) -> dict[str, str]:
    """Derive download headers from upload metadata, safely.

    Content-Disposition is always ``attachment`` so a stored text/html (or
    SVG, …) payload can never execute in the server's origin.
    """
    filetype = (metadata or {}).get("filetype", "")
    if not _MIME_RE.match(filetype):
        filetype = "application/octet-stream"

    filename = (metadata or {}).get("filename", "")
    # Strip CR/LF (header injection) and quotes/backslashes (quoted-string).
    filename = re.sub(r'[\r\n"\\]', "", filename)
    disposition = "attachment"
    if filename:
        if filename.isascii():
            disposition = f'attachment; filename="{filename}"'
        else:
            # Non-Latin-1 names crash stdlib header emission; use the
            # RFC 5987 filename* form with a plain ASCII fallback.
            encoded = quote(filename, safe="")
            disposition = f"attachment; filename=\"download\"; filename*=UTF-8''{encoded}"

    return {"Content-Type": filetype, "Content-Disposition": disposition}


def _plan_get(
    server: TusServerCore, upload_id: str, upload: dict | None
) -> dict | tuple[int, dict[str, str], bytes]:
    """Shared validation for sync/async download; returns the upload or an error."""
    if not upload:
        return server._error_response(404, "Upload not found")

    expires_at = upload.get("expires_at")
    if expires_at and expires_at < datetime.now(timezone.utc):
        return server._error_response(410, "Upload has expired")

    if not upload.get("completed"):
        # Never leak partial content.
        return server._error_response(404, "Upload not complete")

    return upload


def _download_response(
    server: TusServerCore, upload: dict, body: BinaryIO
) -> tuple[int, dict[str, str], BinaryIO]:
    # Completed uploads always know their size; offset covers the (edge)
    # case of legacy rows missing a length.
    size = upload.get("upload_length")
    if size is None:
        size = upload.get("offset", 0)
    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Content-Length": str(size),
        **_content_headers(upload.get("metadata", {})),
    }
    return (200, response_headers, body)


def handle_get_download(
    server: TusServerCore, upload_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes | BinaryIO]:
    upload = server.storage.get_upload(upload_id)
    plan = _plan_get(server, upload_id, upload)
    if not isinstance(plan, dict):
        return plan
    # Stream — never materialize the whole upload in memory (multi-GB files
    # are TUS's whole reason to exist). The transport closes the stream.
    body = server.storage.open_file(upload_id)
    logger.info("Serving download for upload %s", upload_id)
    return _download_response(server, plan, body)


async def handle_get_download_async(
    server: TusServerCore, upload_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes | BinaryIO]:
    """Async sibling of :func:`handle_get_download`."""
    upload = await server.storage.get_upload_async(upload_id)
    plan = _plan_get(server, upload_id, upload)
    if not isinstance(plan, dict):
        return plan
    body = await server.storage.open_file_async(upload_id)
    logger.info("Serving download for upload %s", upload_id)
    return _download_response(server, plan, body)
