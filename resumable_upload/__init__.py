"""Resumable Upload Library

A Python implementation of the TUS resumable upload protocol.
Provides both server and client components with minimal dependencies.
"""

__version__ = "0.0.4"

from resumable_upload.client import TusClient, Uploader, UploadStats
from resumable_upload.exceptions import TusCommunicationError, TusHookError, TusUploadFailed
from resumable_upload.fingerprint import CallableFingerprint, Fingerprint, PartialMD5Fingerprint
from resumable_upload.server import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage, Storage
from resumable_upload.url_storage import (
    FileURLStorage,
    InMemoryURLStorage,
    SQLiteURLStorage,
    URLStorage,
)

# Optional cloud storage backends
try:
    from resumable_upload.storage_s3 import S3Storage
except ImportError:
    S3Storage = None  # type: ignore[assignment,misc]

try:
    from resumable_upload.storage_gcs import GCSStorage
except ImportError:
    GCSStorage = None  # type: ignore[assignment,misc]

try:
    from resumable_upload.storage_azure import AzureBlobStorage
except ImportError:
    AzureBlobStorage = None  # type: ignore[assignment,misc]

__all__ = [
    "TusServer",
    "TusHTTPRequestHandler",
    "TusClient",
    "Uploader",
    "UploadStats",
    "Storage",
    "SQLiteStorage",
    "S3Storage",
    "GCSStorage",
    "AzureBlobStorage",
    "TusCommunicationError",
    "TusUploadFailed",
    "Fingerprint",
    "PartialMD5Fingerprint",
    "CallableFingerprint",
    "URLStorage",
    "FileURLStorage",
    "InMemoryURLStorage",
    "SQLiteURLStorage",
    "TusHookError",
]
