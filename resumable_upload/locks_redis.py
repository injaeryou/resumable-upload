"""Deprecated import path. Use ``resumable_upload.locks.redis_lock`` instead."""

import sys
import warnings

warnings.warn(
    "resumable_upload.locks_redis is deprecated since 0.0.6 and will be "
    "removed in 0.0.8 / 0.1.0; import from "
    "resumable_upload.locks.redis_lock instead.",
    DeprecationWarning,
    stacklevel=2,
)

from resumable_upload.locks import redis_lock as _impl  # noqa: E402

sys.modules[__name__] = _impl
