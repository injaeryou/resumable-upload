"""Header parsing and CORS helpers shared by request handlers."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from email.utils import formatdate

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

TUS_EXPOSE_HEADERS = (
    "Upload-Offset,Location,Upload-Length,Tus-Version,Tus-Resumable,"
    "Tus-Max-Size,Tus-Extension,Upload-Metadata,Upload-Expires,Upload-Concat"
)
TUS_ALLOW_HEADERS = (
    "Origin,X-Requested-With,Content-Type,Upload-Length,Upload-Offset,"
    "Tus-Resumable,Upload-Metadata,Upload-Checksum,Upload-Expires,Upload-Concat"
)

MAX_METADATA_SIZE = 4096


def validate_upload_id(upload_id: str) -> bool:
    """Validate that ``upload_id`` is a UUID, blocking path traversal."""
    return bool(_UUID_RE.match(upload_id))


def add_cors_headers(headers: dict, cors_allow_origins: str | None) -> dict:
    """Mutate ``headers`` in place with CORS values when origins are configured."""
    if cors_allow_origins:
        headers["Access-Control-Allow-Origin"] = cors_allow_origins
        headers["Access-Control-Expose-Headers"] = TUS_EXPOSE_HEADERS
        headers["Access-Control-Allow-Methods"] = "GET,POST,HEAD,PATCH,DELETE,OPTIONS"
        headers["Access-Control-Allow-Headers"] = TUS_ALLOW_HEADERS
    return headers


def format_expiry(expires_at: datetime) -> str:
    """Format an expiry datetime as an RFC 7231 date string."""
    return formatdate(expires_at.timestamp(), usegmt=True)


def parse_metadata(
    upload_metadata: str, max_size: int = MAX_METADATA_SIZE
) -> tuple[dict[str, str] | None, str]:
    """Parse the ``Upload-Metadata`` header value.

    Returns ``(metadata_dict, "")`` on success, or ``(None, error_message)``
    on failure.
    """
    metadata: dict[str, str] = {}
    if not upload_metadata:
        return metadata, ""
    if len(upload_metadata) > max_size:
        return None, f"Upload-Metadata exceeds maximum size of {max_size} bytes"
    for pair in upload_metadata.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if " " in pair:
            key, value = pair.split(" ", 1)
            try:
                metadata[key] = base64.b64decode(value).decode("utf-8")
            except (ValueError, UnicodeDecodeError, binascii.Error) as e:
                return None, f"Invalid base64 encoding for metadata key '{key}': {e}"
        else:
            metadata[pair] = ""
    return metadata, ""
