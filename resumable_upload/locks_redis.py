"""Legacy import path. Prefer ``resumable_upload.locks.redis_lock``.

Aliases this module to the implementation module so that ``importlib.reload``
on the legacy path reloads the canonical module.
"""

import sys

from resumable_upload.locks import redis_lock as _impl

sys.modules[__name__] = _impl
