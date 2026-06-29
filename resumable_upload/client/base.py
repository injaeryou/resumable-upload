"""Deprecated import path. Use ``resumable_upload.client.client`` instead."""

import sys
import warnings

warnings.warn(
    "resumable_upload.client.base is deprecated since 0.0.6 and will be "
    "removed after 0.1.2; import from "
    "resumable_upload.client.client instead.",
    DeprecationWarning,
    stacklevel=2,
)

from resumable_upload.client import client as _impl  # noqa: E402

sys.modules[__name__] = _impl
