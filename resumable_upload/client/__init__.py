"""TUS protocol client implementations."""

from resumable_upload.client.client import TusClient
from resumable_upload.client.stats import UploadStats
from resumable_upload.client.uploader import Uploader

__all__ = ["TusClient", "UploadStats", "Uploader", "AsyncTusClient", "AsyncUploader"]


def __getattr__(name: str) -> object:
    if name in ("AsyncTusClient", "AsyncUploader"):
        import resumable_upload.client.aio as _aio

        return getattr(_aio, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
