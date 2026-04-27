"""Deprecated import path. Use ``resumable_upload.storage.gcs_storage`` instead."""

import sys
import warnings

warnings.warn(
    "resumable_upload.storage_gcs is deprecated since 0.0.6 and will be "
    "removed in 0.0.8 / 0.1.0; import from "
    "resumable_upload.storage.gcs_storage instead.",
    DeprecationWarning,
    stacklevel=2,
)

from resumable_upload.storage import gcs_storage as _impl  # noqa: E402

sys.modules[__name__] = _impl
