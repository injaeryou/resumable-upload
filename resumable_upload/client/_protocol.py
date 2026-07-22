# resumable_upload/client/_protocol.py
"""Pure, transport-agnostic TUS client helpers shared by the sync and async
clients. No urllib / httpx imports — only header strings in, data out."""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from typing import Any

_KEY_RE = re.compile(r"^$|[\s,]+")


def encode_metadata(metadata: dict[str, str], encoding: str) -> list[str]:
    encoded: list[str] = []
    for key, value in metadata.items():
        key_str = str(key)
        if _KEY_RE.search(key_str):
            raise ValueError(
                f'Upload-metadata key "{key_str}" cannot be empty nor contain spaces or commas.'
            )
        encoded.append(f"{key_str} {base64.b64encode(value.encode(encoding)).decode('ascii')}")
    return encoded


def parse_upload_metadata(header_value: str | None, encoding: str) -> dict[str, str]:
    if not header_value:
        return {}
    out: dict[str, str] = {}
    for pair in header_value.split(","):
        pair = pair.strip()
        if " " in pair:
            key, value = pair.split(" ", 1)
            try:
                out[key] = base64.b64decode(value).decode(encoding)
            except (ValueError, UnicodeDecodeError):
                out[key] = value
    return out


def parse_upload_info(
    offset: str | None, length: str | None, metadata: str | None, encoding: str
) -> dict[str, Any]:
    off = int(offset) if offset else 0
    # Distinguish an absent Upload-Length (deferred, length unknown → never
    # complete) from a present "0" (a real zero-length upload, complete at
    # offset 0). Guarding on ``ln > 0`` alone wrongly reports 0-byte uploads
    # as incomplete.
    ln = int(length) if length is not None else 0
    return {
        "offset": off,
        "length": ln,
        "complete": length is not None and off >= ln,
        "metadata": parse_upload_metadata(metadata, encoding),
    }


def parse_server_info(
    version: str | None, extension: str | None, max_size: str | None, default_version: str
) -> dict[str, Any]:
    extensions = [e.strip() for e in extension.split(",") if e.strip()] if extension else []
    return {
        "version": default_version if version is None else version,
        "extensions": extensions,
        "max_size": int(max_size) if max_size else None,
    }


def resolve_checksum_algorithm(checksum: bool | str | None) -> str | None:
    """Normalise the ``checksum`` parameter to a hashlib algorithm name or ``None``."""
    if checksum is False or checksum is None:
        return None
    if checksum is True:
        return "sha1"
    return str(checksum).lower()


def checksum_header(algo: str, data: bytes) -> str:
    """Return an ``Upload-Checksum`` header value for *data* using *algo*."""
    hasher = hashlib.new(algo)
    hasher.update(data)
    return f"{algo} {base64.b64encode(hasher.digest()).decode('ascii')}"


def maybe_add_request_id(headers: dict[str, str], enabled: bool) -> dict[str, str]:
    """Add a per-request ``X-Request-ID`` UUID when enabled.

    A user-supplied X-Request-ID (via custom headers) always wins.
    Mutates and returns ``headers``.
    """
    if enabled and not any(k.lower() == "x-request-id" for k in headers):
        headers["X-Request-ID"] = str(uuid.uuid4())
    return headers


def retry_delay(base: float, attempt: int) -> float:
    """Exponential back-off capped at 60 seconds."""
    return min(base * (2**attempt), 60.0)


def split_boundaries(file_size: int, parts: int) -> list[tuple[int, int]]:
    """Divide *file_size* bytes into at most *parts* non-empty ``(start, end)`` ranges."""
    if file_size == 0 or parts == 0:
        return []
    base = file_size // parts
    out: list[tuple[int, int]] = []
    start = 0
    for i in range(parts):
        end = file_size if i == parts - 1 else start + base
        if end > start:
            out.append((start, end))
        start = end
    return out
