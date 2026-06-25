"""OPTIONS handler — capability discovery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from resumable_upload.server.core import TusServerCore

logger = logging.getLogger(__name__)


def handle_options(
    server: TusServerCore, path: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    logger.debug("Handling OPTIONS request")
    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
        "Tus-Version": server.TUS_VERSION,
        "Tus-Extension": ",".join(server.SUPPORTED_EXTENSIONS),
        "Tus-Checksum-Algorithm": ",".join(server._checksums.enabled),
    }

    if server.max_size > 0:
        response_headers["Tus-Max-Size"] = str(server.max_size)

    return (204, response_headers, b"")


async def handle_options_async(
    server: TusServerCore, path: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    # No storage I/O — defer to the sync implementation.
    return handle_options(server, path, headers)
