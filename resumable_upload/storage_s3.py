"""Legacy import path. Prefer ``resumable_upload.storage.s3_storage``.

Aliases this module to the implementation module so that ``importlib.reload``
on the legacy path reloads the canonical implementation.
"""

import sys

from resumable_upload.storage import s3_storage as _impl

sys.modules[__name__] = _impl
