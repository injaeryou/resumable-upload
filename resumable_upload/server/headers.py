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
    "Tus-Resumable,Upload-Metadata,Upload-Checksum,Upload-Expires,Upload-Concat,"
    "Upload-Defer-Length,X-HTTP-Method-Override,X-Request-ID"
)

MAX_METADATA_SIZE = 4096


def validate_upload_id(upload_id: str) -> bool:
    """Validate that ``upload_id`` is a UUID, blocking path traversal."""
    return bool(_UUID_RE.match(upload_id))


def add_cors_headers(
    headers: dict,
    cors_allow_origins: str | list[str] | None,
    *,
    origin: str | None = None,
    allow_credentials: bool = False,
    max_age: int | None = None,
    preflight: bool = False,
) -> dict:
    """Mutate ``headers`` in place with CORS values when origins are configured.

    ``cors_allow_origins`` accepts a static string (legacy behavior, e.g.
    ``"*"``) or a list of origins matched against the request ``origin``.
    With ``allow_credentials`` a wildcard is replaced by the echoed request
    origin, since ``*`` is invalid alongside credentials.
    """
    if not cors_allow_origins:
        return headers

    origin_dependent = not isinstance(cors_allow_origins, str)
    if isinstance(cors_allow_origins, str):
        allowed: str | None = cors_allow_origins
        if cors_allow_origins == "*" and allow_credentials:
            allowed = origin
            origin_dependent = True
    else:
        allowed = origin if origin in cors_allow_origins else None

    if origin_dependent:
        # Response varies by request Origin — keep caches honest either way,
        # merging with any Vary value another layer already set.
        existing_vary = headers.get("Vary")
        if not existing_vary:
            headers["Vary"] = "Origin"
        elif "origin" not in existing_vary.lower():
            headers["Vary"] = f"{existing_vary}, Origin"
    if not allowed:
        return headers

    headers["Access-Control-Allow-Origin"] = allowed
    headers["Access-Control-Expose-Headers"] = TUS_EXPOSE_HEADERS
    headers["Access-Control-Allow-Methods"] = "GET,POST,HEAD,PATCH,DELETE,OPTIONS"
    headers["Access-Control-Allow-Headers"] = TUS_ALLOW_HEADERS
    if allow_credentials:
        headers["Access-Control-Allow-Credentials"] = "true"
    if preflight and max_age is not None:
        headers["Access-Control-Max-Age"] = str(max_age)
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
        else:
            key, value = pair, ""
        # Spec: keys MUST be ASCII and MUST be unique within the header.
        if not key.isascii():
            return None, f"Upload-Metadata key must be ASCII: '{key}'"
        if key in metadata:
            return None, f"Duplicate Upload-Metadata key: '{key}'"
        if not value:
            metadata[key] = ""
            continue
        try:
            # validate=True: non-alphabet characters are an error, not
            # silently discarded (b64decode('####') == b'' otherwise).
            metadata[key] = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error) as e:
            return None, f"Invalid base64 encoding for metadata key '{key}': {e}"
    return metadata, ""
