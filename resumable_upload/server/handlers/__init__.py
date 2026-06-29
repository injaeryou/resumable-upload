"""Per-method request handlers extracted from :class:`TusServerCore`.

Each ``handle_<method>`` function takes the server instance as its first
argument and returns the ``(status, headers, body)`` triple expected by
``TusServerCore.handle_request``. ``handle_<method>_async`` siblings drive
the same protocol surface through the ``Storage._async`` API so true-async
backends can stay non-blocking end-to-end.
"""

from resumable_upload.server.handlers.create import (
    handle_create,
    handle_create_async,
    handle_create_final,
    handle_create_final_async,
)
from resumable_upload.server.handlers.delete import handle_delete, handle_delete_async
from resumable_upload.server.handlers.head import handle_head, handle_head_async
from resumable_upload.server.handlers.options import handle_options, handle_options_async
from resumable_upload.server.handlers.patch import handle_patch, handle_patch_async

__all__ = [
    "handle_options",
    "handle_options_async",
    "handle_create",
    "handle_create_async",
    "handle_create_final",
    "handle_create_final_async",
    "handle_head",
    "handle_head_async",
    "handle_patch",
    "handle_patch_async",
    "handle_delete",
    "handle_delete_async",
]
