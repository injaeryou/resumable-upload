"""DELETE handler — terminate (and remove) an upload."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from resumable_upload.exceptions import TusHookError

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

    veto = _check_terminate_veto(server, upload_id)
    if veto is not None:
        return veto

    if upload.get("is_partial"):
        blocked = _check_pending_final_refs(
            server, upload_id, server.storage.find_pending_finals_for_partial
        )
        if blocked is not None:
            return blocked

    server.storage.delete_upload(upload_id)
    return _finalize_delete(server, upload_id)


async def handle_delete_async(
    server: TusServerCore, upload_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    upload = await server.storage.get_upload_async(upload_id)
    if not upload:
        logger.warning("Upload not found for deletion: %s", upload_id)
        return server._error_response(404, "Upload not found")

    veto = _check_terminate_veto(server, upload_id)
    if veto is not None:
        return veto

    if upload.get("is_partial"):
        try:
            pending = await server.storage.find_pending_finals_for_partial_async(upload_id)
        except NotImplementedError:
            pending = None
        blocked = _pending_final_refs_response(server, upload_id, pending)
        if blocked is not None:
            return blocked

    await server.storage.delete_upload_async(upload_id)
    return _finalize_delete(server, upload_id)


def _check_terminate_veto(
    server: TusServerCore, upload_id: str
) -> tuple[int, dict[str, str], bytes] | None:
    """pre-terminate: on_before_terminate may veto by raising TusHookError."""
    if not server._on_before_terminate:
        return None
    try:
        server._invoke_pre_hook(server._on_before_terminate, upload_id)
    except TusHookError as e:
        logger.warning("Termination of %s vetoed: %s", upload_id, e.body)
        return (e.status_code, {"Tus-Resumable": server.TUS_VERSION}, e.body.encode())
    return None


def _check_pending_final_refs(
    server: TusServerCore, upload_id: str, finder: Callable[[str], list[str]]
) -> tuple[int, dict[str, str], bytes] | None:
    try:
        pending = finder(upload_id)
    except NotImplementedError:
        pending = None
    return _pending_final_refs_response(server, upload_id, pending)


def _pending_final_refs_response(
    server: TusServerCore, upload_id: str, pending: list[str] | None
) -> tuple[int, dict[str, str], bytes] | None:
    """409 when a pending (unassembled) final still needs this partial.

    Deleting it would strand the final forever: try_assemble_final can never
    complete once a source partial is gone.
    """
    if not pending:
        return None
    logger.warning(
        "Refusing to delete partial %s: referenced by pending final(s) %s",
        upload_id,
        ", ".join(pending),
    )
    return server._error_response(
        409, "Partial upload is referenced by an unfinished concatenation"
    )


def _finalize_delete(server: TusServerCore, upload_id: str) -> tuple[int, dict[str, str], bytes]:
    logger.info("Deleted upload %s", upload_id)
    if server._metrics is not None:
        server._metrics.inc("tusd_uploads_terminated_total")

    if server._on_upload_terminate:
        server._invoke_post_hook(server._on_upload_terminate, upload_id)

    response_headers = {
        "Tus-Resumable": server.TUS_VERSION,
    }
    return (204, response_headers, b"")
