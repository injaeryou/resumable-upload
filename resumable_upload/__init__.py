"""Resumable Upload Library

A Python implementation of the TUS resumable upload protocol.
Provides both server and client components with minimal dependencies.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Version comes from the git tag at build time (setuptools-scm); read it back
# from the installed package metadata so the two can never drift.
try:
    __version__ = _pkg_version("resumable-upload")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0.0.0+unknown"
del _pkg_version, PackageNotFoundError

from resumable_upload.client import TusClient, Uploader, UploadStats
from resumable_upload.exceptions import TusCommunicationError, TusHookError, TusUploadFailed
from resumable_upload.fingerprint import CallableFingerprint, Fingerprint, PartialMD5Fingerprint
from resumable_upload.server import TusHTTPRequestHandler, TusServer, TusServerCore
from resumable_upload.storage import SQLiteStorage, Storage
from resumable_upload.url_storage import (
    FileURLStorage,
    InMemoryURLStorage,
    SQLiteURLStorage,
    URLStorage,
)

# Optional cloud storage backends
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
    "TusServerCore",
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
    "AsyncTusClient",
    "AsyncUploader",
]


def __getattr__(name: str) -> object:
    if name in ("AsyncTusClient", "AsyncUploader"):
        import resumable_upload.client.aio as _aio

        return getattr(_aio, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
