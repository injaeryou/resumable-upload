"""Default TUS server class.

``TusServer`` inherits from :class:`TusServerCore` and is the canonical class
to instantiate. It exists as a distinct subclass so future extensions can
land here without forcing every downstream user to update imports.
"""

from resumable_upload.server.core import TusServerCore


class TusServer(TusServerCore):
    """TUS server with all standard extensions enabled.

    Behaviorally identical to :class:`TusServerCore` today. Reserve
    ``TusServerCore`` for downstream code that wants to subclass against the
    minimal surface; reserve ``TusServer`` for users who want the canonical
    server with whatever extensions ship by default in future releases.
    """
