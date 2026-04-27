"""Legacy import path. Prefer ``resumable_upload.client.client``.

Aliases this module to the implementation module so that mocks targeting
the legacy path (``resumable_upload.client.base.urlopen``) keep working.
"""

import sys

from resumable_upload.client import client as _impl

sys.modules[__name__] = _impl
