"""DELETE handler — terminate (and remove) an upload."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from resumable_upload.server.core import TusServerCore

logger = logging.getLogger(__name__)


def handle_delete(
    server: TusServerCore, upload_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    upload = server.storage.get_upload(upload_id)
    if not upload:
        logger.warning("Upload not found for deletion: %s", upload_id)
        return server._error_response(404, "Upload not found")

    server.storage.delete_upload(upload_id)
    logger.info("Deleted upload %s", upload_id)
    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_terminated_total")

    if server._on_upload_terminate:
        server._invoke_post_hook(server._on_upload_terminate, upload_id)

    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
    }
    return (204, response_headers, b"")
