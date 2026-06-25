"""Per-method request handlers extracted from :class:`TusServerCore`.

Each ``handle_<method>`` function takes the server instance as its first
argument and returns the ``(status, headers, body)`` triple expected by
``TusServerCore.handle_request``.
"""

from resumable_upload.server.handlers.create import handle_create
from resumable_upload.server.handlers.delete import handle_delete
from resumable_upload.server.handlers.head import handle_head
from resumable_upload.server.handlers.options import handle_options
from resumable_upload.server.handlers.patch import handle_patch

__all__ = [
    "handle_options",
    "handle_create",
    "handle_head",
    "handle_patch",
    "handle_delete",
]
