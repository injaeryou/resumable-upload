"""Storage backends for managing upload state.

The :class:`Storage` ABC and the default :class:`SQLiteStorage` implementation
are always available. Cloud-backed storage classes are imported lazily and
become ``None`` when their optional SDK is not installed; users should check
for ``None`` before instantiating.
"""

from resumable_upload.storage.base import Storage
from resumable_upload.storage.sqlite_storage import SQLiteStorage

try:
    from resumable_upload.storage.s3_storage import S3Storage
except ImportError:
    S3Storage = None  # type: ignore[assignment,misc]

try:
    from resumable_upload.storage.gcs_storage import GCSStorage
except ImportError:
    GCSStorage = None  # type: ignore[assignment,misc]

try:
    from resumable_upload.storage.azure_storage import AzureBlobStorage
except ImportError:
    AzureBlobStorage = None  # type: ignore[assignment,misc]

__all__ = [
    "Storage",
    "SQLiteStorage",
    "S3Storage",
    "GCSStorage",
    "AzureBlobStorage",
]
